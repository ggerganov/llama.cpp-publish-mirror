#!/usr/bin/env python3
import json
import urllib.request
import urllib.parse
import base64
import os

# This script mirrors a set of tags from one repository to another.
# It is designed mirror WITHIN the same registry, by cross-mounting blobs.
# This prevents the need to download and re-upload the same data.

REGISTRY_HOST = "ghcr.io"
REGISTRY_SERVICE = "ghcr.io"

SOURCE_REPO = "ggml-org/llama.cpp"
TARGET_REPO = "ggerganov/llama.cpp"
SYNC_TAGS = [
    'server', 'light', 'full',
    'server-intel', 'light-intel', 'full-intel',
    'server-cuda', 'light-cuda', 'full-cuda',
    'server-musa', 'light-musa', 'full-musa',
    'server-vulkan', 'light-vulkan', 'full-vulkan'
]

####################################################################
# Registry API functions

def get_auth_token(repository: str, scope: str, host=REGISTRY_HOST, service=REGISTRY_SERVICE, credentials=None) -> str:
    """
    Retrieves an authentication token for the specified repository.

    For Docker Hub, the token endpoint is used with parameters:
      - host: auth.docker.io
      - service: registry.docker.io

    For ghcr.io the token endpoint is: https://ghcr.io/token?service=ghcr.io&scope=repository:<repository>:pull
      - host: ghcr.io
      - service: ghcr.io
    """
    token_url = f'https://{host}/token'
    query_params = {
        'service': service,
        'scope': f'repository:{repository}:{scope}'
    }
    url = token_url + '?' + urllib.parse.urlencode(query_params)
    req = urllib.request.Request(url)

    if credentials is not None:
        username, password = credentials
        credentials_str = f"{username}:{password}"
        encoded_credentials = base64.b64encode(credentials_str.encode('utf-8')).decode('utf-8')
        req.add_header('Authorization', f'Basic {encoded_credentials}')

    with urllib.request.urlopen(req) as response:
        data = response.read()

    token_data = json.loads(data.decode('utf-8'))
    return token_data['token']

def fetch_manifest(repository: str, tag: str, token: str, host=REGISTRY_HOST) -> str:
    """
    Fetches the manifest for the given repository and tag using the provided token.
    For docker hub, host is registry-1.docker.io
    """
    manifest_url = f'https://{host}/v2/{repository}/manifests/{tag}'
    req = urllib.request.Request(manifest_url)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json')

    with urllib.request.urlopen(req) as response:
        data = response.read()

    return data.decode('utf-8')

def cross_mount_blob(dest_repo: str, source_repo: str, digest: str, token: str, host=REGISTRY_HOST):
    """
    Attempts to cross-mount (copy) a blob from source_repo into dest_repo on ghcr.io.
    """
    # Build the URL: POST /v2/<dest_repo>/blobs/uploads/?mount=<digest>&from=<source_repo>
    base_url = f'https://{host}/v2/{dest_repo}/blobs/uploads/'
    params = {
        'mount': digest,
        'from': source_repo
    }
    url = base_url + '?' + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, method="POST")
    req.add_header('Authorization', f'Bearer {token}')

    with urllib.request.urlopen(req) as response:
        status = response.getcode()
        resp_body = response.read().decode('utf-8')
    return status, resp_body

# Helper class to allow HTTP PUT
class PutRequest(urllib.request.Request):
    def get_method(self):
        return "PUT"

def push_manifest(dest_repository: str, digest: str, data: str, token: str, src_cross_mount: str, host=REGISTRY_HOST):
    """
    Pushes a manifest (as JSON) to the destination repository.
    """
    url = f'https://{host}/v2/{dest_repository}/manifests/{digest}'
    print("PUT", url)
    manifest_json = json.loads(data)
    for layer in manifest_json.get("layers", []):
        layer_digest = layer.get("digest")
        print(f"    Attempting to cross-mount layer {layer_digest}")
        cross_mount_blob(dest_repository, src_cross_mount, layer_digest, token)
    media_type = manifest_json.get("mediaType", "application/vnd.oci.image.index.v1+json")
    req = PutRequest(url, data=data.encode('utf-8'))
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', media_type)
    with urllib.request.urlopen(req) as response:
        status = response.getcode()
        resp_body = response.read().decode('utf-8')
    return status, resp_body

def untag_manifest(repository: str, tag: str, token: str, host=REGISTRY_HOST):
    """
    Untags a manifest from the repository.
    """
    url = f'https://{host}/v2/{repository}/manifests/{tag}'
    req = urllib.request.Request(url, method="DELETE")
    req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req) as response:
        status = response.getcode()
        resp_body = response.read().decode('utf-8')
    return status, resp_body

####################################################################
# Main function

def mirror_image(src_repo, src_ref, dest_repo, dest_tag, token_pull, token_push):
    print(f"Fetching manifest for {src_repo}:{src_ref}")
    manifest_raw = fetch_manifest(src_repo, src_ref, token_pull)
    manifest_json = json.loads(manifest_raw)

    # Check if this is a multi-arch image index.
    if "manifests" in manifest_json:
        print("Detected multi-arch manifest index.")
        # For each sub-manifest, mirror it.
        for entry in manifest_json["manifests"]:
            sub_digest = entry.get("digest")
            platform = entry.get("platform", {})
            print(f"  Mirroring sub-manifest for platform {platform} with digest {sub_digest}")
            sub_manifest = fetch_manifest(src_repo, sub_digest, token_pull)
            print(f"  Got sub-manifest:")
            print(json.dumps(json.loads(sub_manifest), indent=2))
            status_sub, resp_sub = push_manifest(dest_repo, sub_digest, sub_manifest, token_push, src_repo)
            print(f"  Pushed sub-manifest {sub_digest}: HTTP {status_sub}", resp_sub)
    else:
        print("Single-architecture manifest detected:")
        print(json.dumps(manifest_json, indent=2))

    print("\nPushing top-level manifest (index or single manifest) to destination")
    status, response_body = push_manifest(dest_repo, dest_tag, manifest_raw, token_push, src_repo)

    # Untag the dest manifest if needed (so they will apprear under "untagged" in the registry)
    # TODO: this doesn't work, DELETE /v2/{repository}/manifests/{tag} does not exist
    # if "manifests" in manifest_json:
    #     for entry in manifest_json["manifests"]:
    #         sub_digest = entry.get("digest")
    #         status_sub, resp_sub = untag_manifest(dest_repo, sub_digest, token_push)
    #         print(f"  Untagged sub-manifest {sub_digest}: HTTP {status_sub}", resp_sub)

    return status, response_body

for tag in SYNC_TAGS:
    # get token for pushing
    push_username = os.environ.get('PUSH_USERNAME')
    if not push_username:
        print("Error: PUSH_USERNAME environment variable not set")
    push_password = os.environ.get('PUSH_PASSWORD')
    if not push_password:
        print("Error: PUSH_PASSWORD environment variable not set")
    token_push = get_auth_token(SOURCE_REPO, 'push', credentials=(push_username, push_password))

    # get token for pulling
    token_pull = get_auth_token(SOURCE_REPO, 'pull')

    try:
        status, resp = mirror_image(SOURCE_REPO, tag, TARGET_REPO, tag, token_pull, token_push)
        print(f"\nMirror push response: HTTP {status}")
        print(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Error: {e}", body)
        exit(1)
    except Exception as e:
        print(f"Error: {e}")
        exit(1)

