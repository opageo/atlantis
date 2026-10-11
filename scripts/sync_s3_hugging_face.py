import argparse
from pathlib import Path

from huggingface_hub import HfApi

SOURCE_BUCKET = "atlantis"
ARCHIVE_PREFIX = "zarr/archive/"
DEFAULT_LOCAL_ROOT = "/mnt/data/s3/atlantis/zarr/archive/"
HF_REPO_ID = "opageo/atlantis"

# Sub-path under the archive root -> destination folder in the HF dataset repo
SYNC_MAP = {
    "datacube.zarr": "data.zarr",
    "stac": "stac",
}


def sync_from_s3(hf_api: HfApi) -> None:
    """Upload object by object from S3 (one HTTP commit per object)."""
    import boto3

    s3_client = boto3.client("s3")
    paginator = s3_client.get_paginator("list_objects_v2")

    for sub_path, repo_dir in SYNC_MAP.items():
        prefix = f"{ARCHIVE_PREFIX}{sub_path}/"
        pages = paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix)

        for page in pages:
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]

                path_in_repo = s3_key[len(prefix) :]
                if not path_in_repo or path_in_repo.endswith("/"):
                    continue

                response = s3_client.get_object(Bucket=SOURCE_BUCKET, Key=s3_key)
                file_body = response["Body"].read()

                hf_api.upload_file(
                    path_or_fileobj=file_body,
                    path_in_repo=f"{repo_dir}/{path_in_repo}",
                    repo_id=HF_REPO_ID,
                    repo_type="dataset",
                )


def sync_from_local(hf_api: HfApi, local_root: Path) -> None:
    """Upload each mapped directory from a local mirror in a single commit."""
    for sub_path, repo_dir in SYNC_MAP.items():
        folder = local_root / sub_path
        if not folder.is_dir():
            print(f"Skipping missing directory: {folder}")
            continue

        print(f"Uploading {folder} -> {HF_REPO_ID}:{repo_dir}")
        hf_api.upload_folder(
            folder_path=folder,
            path_in_repo=repo_dir,
            repo_id=HF_REPO_ID,
            repo_type="dataset",
            commit_message=f"Sync {sub_path} from {local_root}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the Atlantis archive to Hugging Face.")
    parser.add_argument(
        "--mode",
        choices=["s3", "local"],
        default="local",
        help="'s3': iterate S3 objects; 'local': upload whole directories from a local mirror.",
    )
    parser.add_argument(
        "--local-root",
        type=Path,
        default=Path(DEFAULT_LOCAL_ROOT),
        help=f"Local archive root used in 'local' mode (default: {DEFAULT_LOCAL_ROOT}).",
    )
    args = parser.parse_args()

    hf_api = HfApi()
    if args.mode == "s3":
        sync_from_s3(hf_api)
    else:
        sync_from_local(hf_api, args.local_root)


if __name__ == "__main__":
    main()
