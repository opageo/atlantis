"""List all collections available from two STAC endpoints."""

from pystac_client import Client

ENDPOINTS = {
    "Copernicus Dataspace": "https://stac.dataspace.copernicus.eu/v1",
    "EODC": "https://stac.eodc.eu/api/v1",
}


def list_collections(name: str, url: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"  {url}")
    print("=" * 60)

    cat = Client.open(url)
    collections = list(cat.get_collections())
    print(f"  {len(collections)} collection(s) found:\n")
    for col in sorted(collections, key=lambda c: c.id):
        print(f"  - {col.id}")
        if col.description:
            # Truncate long descriptions to a single line
            desc = col.description.splitlines()[0][:100]
            print(f"      {desc}")


def main() -> None:
    for name, url in ENDPOINTS.items():
        try:
            list_collections(name, url)
        except Exception as exc:
            print(f"\n[ERROR] {name} ({url}): {exc}")


if __name__ == "__main__":
    main()
