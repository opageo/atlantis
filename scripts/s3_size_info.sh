#!/usr/bin/env bash
# Get size and file count for S3 paths (fast, uses s3api)
# Usage: ./s3_size_info.sh <path>
# Examples:
#   ./s3_size_info.sh 2025
#   ./s3_size_info.sh zarr/viirs_archive
#   ./s3_size_info.sh s3://atlantis/zarr/2025

set -euo pipefail

ENDPOINT_URL="https://object-store.os-api.cci1.ecmwf.int"
BUCKET="atlantis"
BASE_PREFIX="zarr"

usage() {
    cat << EOF
Usage: $0 [OPTIONS] <path>

Get S3 object count and total size (fast, uses s3api).

Arguments:
  <path>              S3 path (relative to s3://atlantis/, or full s3:// URI)
                      Examples: "2025", "zarr/2025", "s3://atlantis/zarr/2025"

Options:
  -e, --endpoint-url  S3 endpoint URL (default: $ENDPOINT_URL)
  -b, --bucket        S3 bucket (default: $BUCKET)
  -p, --prefix        Base prefix (default: $BASE_PREFIX)
  -h, --help          Show this help

Examples:
  $0 2025
  $0 zarr/viirs_archive
  $0 s3://atlantis/zarr/viirs_2024q4
EOF
    exit "${1:-0}"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -e|--endpoint-url)
            ENDPOINT_URL="$2"
            shift 2
            ;;
        -b|--bucket)
            BUCKET="$2"
            shift 2
            ;;
        -p|--prefix)
            BASE_PREFIX="$2"
            shift 2
            ;;
        -h|--help)
            usage 0
            ;;
        -*)
            echo "Error: Unknown option $1" >&2
            usage 1
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -eq 0 ]]; then
    echo "Error: path argument required" >&2
    usage 1
fi

PATH_ARG="$1"

# Normalize path: strip s3://bucket/ prefix and handle various formats
S3_PATH="${PATH_ARG#s3://*/}"  # Remove s3://anything/ prefix
S3_PATH="${S3_PATH#$BUCKET/}"  # Remove bucket prefix if present

# If path doesn't start with base prefix, add it
if ! [[ "$S3_PATH" =~ ^$BASE_PREFIX/ ]]; then
    S3_PATH="$BASE_PREFIX/$S3_PATH"
fi

# Remove trailing slash for consistency
S3_PATH="${S3_PATH%/}"

# Use s3api for efficiency (doesn't require --recursive, paginated automatically)
RESULT=$(aws s3api list-objects-v2 \
    --bucket "$BUCKET" \
    --prefix "$S3_PATH/" \
    --endpoint-url "$ENDPOINT_URL" \
    --query '[sum(Contents[].Size), length(Contents[])]' \
    --output text 2>/dev/null || echo "0 0")

read -r TOTAL_BYTES TOTAL_FILES <<< "$RESULT"

if [[ "$TOTAL_BYTES" == "None" || "$TOTAL_BYTES" == "0" ]]; then
    TOTAL_BYTES=0
    TOTAL_FILES=0
fi

TOTAL_GB=$(awk "BEGIN {printf \"%.2f\", $TOTAL_BYTES / 1024 / 1024 / 1024}")

echo "Path: s3://$BUCKET/$S3_PATH"
echo "Size: $TOTAL_GB GB ($TOTAL_BYTES bytes)"
echo "Files: $TOTAL_FILES"
