#!/usr/bin/env bash
# Batch query S3 sizes
# Usage: ./s3_size_batch.sh <store1> <store2> ...
# Example: ./s3_size_batch.sh 2025 viirs_archive viirs_2024q4

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 <store1> <store2> ..."
    echo "Example: $0 2025 viirs_archive viirs_2024q4"
    exit 1
fi

for store in "$@"; do
    echo -n "$store: "
    "$SCRIPT_DIR/s3_size_info.sh" "$store" | awk '
        /Size:/ {
            size = $2
            files_line = NR
        }
        /Files:/ && NR > files_line {
            printf "%s, %s files\n", size, $2
            exit
        }
    '
done
