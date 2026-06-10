"""Bootstrap utilities for Atlantis data assets.

Provides a reusable ``run_setup`` function used by both the CLI
(``atlantis setup``) and the standalone ``scripts/setup.py`` entry point.

The function walks a registry of required assets and for each one:
  * skips if the file already exists
  * attempts an automatic ``git restore`` when the file is tracked but missing
  * falls back to a manual instruction when auto-restore fails
  * verifies SHA-256 integrity against ``config/asset_hashes.json``

Only assets required by the core workflow are registered here.  Optional
assets (e.g. the KuroSiwo catalogue) are downloaded on demand via
dedicated scripts, not as a global prerequisite.

It also walks a registry of required credentials (e.g. NASA Earthdata
bearer token) and, when running interactively, prompts the user to enter
any missing values and persists them to ``.env`` at the repo root.

Finally, it walks a registry of expected AWS profiles (e.g. ECMWF object
store, anonymous AWS for NOAA buckets) and offers to create / update them
in ``~/.aws/{config,credentials}``.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ASSET_HASHES_PATH = _REPO_ROOT / "config" / "asset_hashes.json"
_ENV_PATH = _REPO_ROOT / ".env"
_AWS_CONFIG_PATH = Path.home() / ".aws" / "config"
_AWS_CREDENTIALS_PATH = Path.home() / ".aws" / "credentials"


# ── Asset registry ─────────────────────────────────────────────────────────
# Each entry: (label, relative path from repo root, tracked_in_git?)
#
# Only assets required by the core workflow (e.g. ``atlantis demo``) are
# listed here.  Optional LFS assets (e.g. KuroSiwo catalogue) are not
# included — they are validated on demand by the commands that need them.

ASSETS: list[tuple[str, Path, bool]] = [
    (
        "VIIRS AOI tile grid",
        Path("src/atlantis/fetchers/viirs/data/viirs_aois.geojson"),
        True,
    ),
]

# Assets that should be verified against the expected hash (excludes LFS).
_HASHED_ASSETS: frozenset[str] = frozenset(
    {
        "src/atlantis/fetchers/viirs/data/viirs_aois.geojson",
    }
)


# ── Hash helpers ───────────────────────────────────────────────────────────


def _compute_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_asset_hashes() -> dict[str, str]:
    """Load expected hashes from ``config/asset_hashes.json``.

    Returns an empty dict when the file does not exist.
    """
    if not _ASSET_HASHES_PATH.exists():
        return {}
    with open(_ASSET_HASHES_PATH) as fh:
        return json.load(fh)


def _asset_expected_hash(rel_path: str) -> str | None:
    """Return the ``sha256:<hex>`` value for *rel_path*, or ``None``."""
    hashes = _load_asset_hashes()
    return hashes.get(rel_path)


def _write_asset_hashes(hashes: dict[str, str]) -> None:
    """Write (or overwrite) ``config/asset_hashes.json``."""
    _ASSET_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ASSET_HASHES_PATH, "w") as fh:
        json.dump(hashes, fh, indent=2)
        fh.write("\n")


# ── File helpers ───────────────────────────────────────────────────────────


def _is_lfs_pointer(path: Path) -> bool:
    """Return True if *path* looks like a Git-LFS pointer instead of real data."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
        return head.startswith(b"version https://git-lfs.github.com/spec/v1")
    except OSError:
        return False


def _git_restore(path: Path) -> bool:
    """Try to restore *path* from the current git tree.  Returns True on success."""
    if shutil.which("git") is None:
        return False
    try:
        result = subprocess.run(
            ["git", "restore", str(path)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_geo_dependency() -> bool:
    """Return True if the ``geo`` extra dependencies are importable."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401

        return True
    except ImportError:
        return False


def _check_hdf4_support() -> bool:
    """Return True if the system GDAL exposes the HDF4 driver.

    Required by the MODIS ``laads_hdf4`` backend (historical reprocessed
    archive, 2003–2025). The LANCE GeoTIFF backend does not need this.
    """
    try:
        from osgeo import gdal  # type: ignore[import-not-found]

        driver_names = {gdal.GetDriver(i).ShortName for i in range(gdal.GetDriverCount())}
        return "HDF4" in driver_names or "HDF4Image" in driver_names
    except ImportError:
        pass
    try:
        import rasterio

        drivers = set(rasterio.drivers.raster_driver_extensions().values())
        return "HDF4" in drivers or "HDF4Image" in drivers
    except Exception:
        return False


# ── Credentials registry ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Credential:
    """A required credential / API token.

    Attributes:
        label: Human-readable name shown in the setup output.
        env_var: Environment variable consulted at runtime.
        required_for: Short list of source IDs that need this credential.
        signup_url: Where the user can obtain the value.
        prompt: One-line prompt shown when asking interactively.
    """

    label: str
    env_var: str
    required_for: tuple[str, ...]
    signup_url: str
    prompt: str


CREDENTIALS: list[Credential] = [
    Credential(
        label="NASA Earthdata bearer token",
        env_var="EARTHDATA_TOKEN",
        required_for=("modis",),
        signup_url="https://urs.earthdata.nasa.gov/",
        prompt="Paste your Earthdata token (input hidden, leave blank to skip)",
    ),
]


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` ``.env`` file.

    Ignores blank lines and ``#`` comments. Strips matching surrounding
    single/double quotes around values. Does not handle multi-line values.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _upsert_env_file(path: Path, key: str, value: str) -> None:
    """Insert-or-update ``key=value`` in a simple ``.env`` file.

    Restricts the file to user-only permissions (``0600``) after writing
    so secrets are not world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted = f"{key}='{value}'\n"

    if not path.exists():
        path.write_text(quoted)
    else:
        lines = path.read_text().splitlines(keepends=True)
        found = False
        for i, raw_line in enumerate(lines):
            stripped = raw_line.lstrip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            existing_key, _, _ = stripped.partition("=")
            if existing_key.strip() == key:
                lines[i] = quoted
                found = True
                break
        if not found:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines.append(quoted)
        path.write_text("".join(lines))

    # Best-effort: tighten perms to user-only (no-op on Windows).
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _resolve_credential(cred: Credential, env_file_values: dict[str, str]) -> str | None:
    """Return the active value for *cred*, preferring process env over ``.env``."""
    value = os.environ.get(cred.env_var, "").strip()
    if value:
        return value
    file_value = env_file_values.get(cred.env_var, "").strip()
    return file_value or None


def _check_credentials(
    *,
    interactive: bool,
    output_print,
) -> bool:
    """Verify credentials are set; prompt for missing ones when *interactive*.

    Returns True when every credential has a value (env or .env) at the
    end of the check; False when at least one is still missing.
    """
    env_file_values = _read_env_file(_ENV_PATH)
    all_ok = True

    for cred in CREDENTIALS:
        current = _resolve_credential(cred, env_file_values)

        if current:
            source = "env" if os.environ.get(cred.env_var, "").strip() else ".env"
            output_print(f"[bold green]✓[/bold green]  {cred.label} (${cred.env_var}, from {source})")
            # Mirror the .env value into the running process so subsequent
            # code (e.g. the CLI calling run_setup) picks it up immediately.
            if source == ".env":
                os.environ[cred.env_var] = current
            continue

        sources = ", ".join(cred.required_for)
        output_print(f"[bold yellow]⚠[/bold yellow]  {cred.label} not configured (${cred.env_var})")
        output_print(f"       Required for: {sources}")
        output_print(f"       Register at: {cred.signup_url}")

        if not interactive:
            output_print(f"       Set it via: export {cred.env_var}='YOUR_TOKEN'")
            output_print(f"       Or add to {_ENV_PATH.relative_to(_REPO_ROOT)}: {cred.env_var}=YOUR_TOKEN")
            all_ok = False
            continue

        # Interactive prompt — getpass hides the input so the secret never
        # appears on screen or in shell history.
        try:
            entered = getpass(f"       {cred.prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            output_print("\n       [yellow]Skipped (no input).[/yellow]")
            all_ok = False
            continue

        if not entered:
            output_print("       [yellow]Skipped — fetchers that need this credential will fail.[/yellow]")
            all_ok = False
            continue

        try:
            _upsert_env_file(_ENV_PATH, cred.env_var, entered)
        except OSError as exc:
            output_print(f"       [red]Failed to write {_ENV_PATH}: {exc}[/red]")
            all_ok = False
            continue

        os.environ[cred.env_var] = entered
        output_print(f"       [green]Saved to {_ENV_PATH.relative_to(_REPO_ROOT)}[/green]")

    return all_ok


# ── AWS profile registry ──────────────────────────────────────────────────


@dataclass(frozen=True)
class AwsProfile:
    """An expected ``~/.aws/{config,credentials}`` profile.

    Attributes:
        name: Profile name as referenced by ``boto3.Session(profile_name=…)``.
        description: Short human-readable description shown during setup.
        used_by: Short list of features that depend on this profile.
        endpoint_url: Optional custom S3 endpoint (e.g. ECMWF object store).
            None means use AWS defaults.
        region: AWS region (defaults to ``us-east-1`` when omitted).
        anonymous: When True, the profile is unsigned (no credentials needed,
            no ``~/.aws/credentials`` section beyond an empty header).
        ca_bundle: Optional path to a custom CA bundle (defaults to system).
        config_extras: Additional key=value pairs for ``~/.aws/config``.
    """

    name: str
    description: str
    used_by: tuple[str, ...]
    endpoint_url: str | None = None
    region: str = "us-east-1"
    anonymous: bool = False
    ca_bundle: str | None = None
    config_extras: dict[str, str] = field(default_factory=dict)


# Atlantis-recognised AWS profiles. Keeping this short and named — the
# ``default`` profile is the ECMWF object store (auth required); ``noa`` is an
# anonymous AWS profile for public boto3 access to NOAA-hosted buckets.
AWS_PROFILES: list[AwsProfile] = [
    AwsProfile(
        name="default",
        description="ECMWF object store (s3://atlantis/ reference data, GFM E2E tests)",
        used_by=("gfm e2e tests", "stac demo", "reference outputs"),
        endpoint_url="https://object-store.os-api.cci1.ecmwf.int",
        region="us-east-1",
        anonymous=False,
        # Linux distros vary on the bundle path; only set when present.
        ca_bundle=("/etc/pki/tls/certs/ca-bundle.crt" if Path("/etc/pki/tls/certs/ca-bundle.crt").exists() else None),
    ),
    AwsProfile(
        name="noa",
        description="Public NOAA buckets — region/endpoint isolated from [default]",
        used_by=("aws s3 ls --profile noa --no-sign-request", "anonymous boto3"),
        endpoint_url=None,
        region="us-east-1",
        anonymous=True,
    ),
]


def _read_ini(path: Path) -> configparser.RawConfigParser:
    """Load an INI file, returning an empty parser when the file is missing."""
    # Use RawConfigParser so values like 's3 =\n    signature_version = unsigned'
    # round-trip without interpolation surprises.
    parser = configparser.RawConfigParser()
    if path.exists():
        parser.read(path)
    return parser


def _write_ini(path: Path, parser: configparser.RawConfigParser, *, secret: bool) -> None:
    """Persist *parser* to *path* with safe permissions on secret files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        parser.write(fh)
    try:
        os.chmod(path, 0o600 if secret else 0o644)
    except OSError:
        pass


def _config_section_name(profile: AwsProfile) -> str:
    """Return the ``~/.aws/config`` section name for *profile*.

    The AWS spec uses ``[default]`` for the default profile and
    ``[profile <name>]`` for everything else.
    """
    return "default" if profile.name == "default" else f"profile {profile.name}"


def _profile_already_configured(
    profile: AwsProfile,
    config_parser: configparser.RawConfigParser,
    creds_parser: configparser.RawConfigParser,
) -> bool:
    """Return True when the profile is present and has the expected shape.

    For anonymous profiles, only the config section is required. For
    credentialed profiles, both ``~/.aws/credentials[<name>]`` and a
    non-empty ``aws_access_key_id`` are required.
    """
    config_section = _config_section_name(profile)
    if not config_parser.has_section(config_section):
        return False
    if profile.anonymous:
        return True
    if not creds_parser.has_section(profile.name):
        return False
    return bool(creds_parser.get(profile.name, "aws_access_key_id", fallback="").strip())


def _profile_endpoint_matches(
    profile: AwsProfile,
    config_parser: configparser.RawConfigParser,
) -> bool:
    """True when an existing profile already advertises the expected endpoint_url.

    Anonymous profiles and profiles with no expected endpoint always match.
    """
    if profile.anonymous or not profile.endpoint_url:
        return True
    section = _config_section_name(profile)
    current = config_parser.get(section, "endpoint_url", fallback="").strip()
    return current == profile.endpoint_url


def _apply_profile_to_parsers(
    profile: AwsProfile,
    config_parser: configparser.RawConfigParser,
    creds_parser: configparser.RawConfigParser,
    *,
    access_key: str | None,
    secret_key: str | None,
) -> None:
    """Insert / update sections in both parsers in-place.

    Existing credential keys are only overwritten when *access_key* /
    *secret_key* are explicitly supplied. All other Atlantis-managed
    config keys (region, endpoint_url, ca_bundle, anonymous s3 block,
    config_extras) are always written. Unknown keys in the existing
    sections are preserved.
    """
    config_section = _config_section_name(profile)
    if not config_parser.has_section(config_section):
        config_parser.add_section(config_section)
    config_parser.set(config_section, "region", profile.region)
    if profile.endpoint_url:
        config_parser.set(config_section, "endpoint_url", profile.endpoint_url)
    if profile.ca_bundle:
        config_parser.set(config_section, "ca_bundle", profile.ca_bundle)
    if profile.anonymous:
        # Multi-line value: ConfigParser accepts indented continuation lines.
        config_parser.set(config_section, "s3", "\n    signature_version = unsigned")
    for key, value in profile.config_extras.items():
        config_parser.set(config_section, key, value)

    # Credentials file: write a section even for anonymous profiles so users
    # can swap ``profile_name="noa"`` without ``ProfileNotFound``.
    if not creds_parser.has_section(profile.name):
        creds_parser.add_section(profile.name)
    if not profile.anonymous and access_key and secret_key:
        creds_parser.set(profile.name, "aws_access_key_id", access_key)
        creds_parser.set(profile.name, "aws_secret_access_key", secret_key)


def _prompt_credentials(profile_name: str, output_print) -> tuple[str, str] | None:
    """Interactively collect access/secret keys via ``getpass``.

    Returns ``(access, secret)`` on success, or ``None`` when the user
    skipped or one of the values was empty.
    """
    try:
        access_prompt = f"       Access key for AWS profile '{profile_name}' (hidden, blank to skip): "
        access_key = getpass(access_prompt).strip()
        if not access_key:
            output_print("       [yellow]Skipped — features that need this profile will fail.[/yellow]")
            return None
        secret_key = getpass(f"       Secret key for AWS profile '{profile_name}' (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        output_print("\n       [yellow]Skipped (no input).[/yellow]")
        return None

    if not secret_key:
        output_print("       [yellow]Skipped — secret key cannot be empty.[/yellow]")
        return None

    return access_key, secret_key


def _prompt_yes(question: str, default: bool = False) -> bool:
    """Tiny y/N prompt (default is shown in brackets)."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"       {question} {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


def _check_aws_profiles(
    *,
    interactive: bool,
    output_print,
) -> bool:
    """Verify the expected AWS profiles exist; prompt to create / repair them.

    Three paths per profile:
      1. fully configured + endpoint matches → reported as OK
      2. fully configured but endpoint mismatches → offer to **repair** (set
         the expected endpoint_url / ca_bundle; existing keys preserved
         unless the user explicitly wants to replace them)
      3. missing entirely → prompt to create

    Returns True when every profile is configured at the end of the check;
    False when at least one is still missing or unrepaired.
    """
    config_parser = _read_ini(_AWS_CONFIG_PATH)
    creds_parser = _read_ini(_AWS_CREDENTIALS_PATH)
    all_ok = True
    dirty_config = False
    dirty_creds = False

    for profile in AWS_PROFILES:
        used_by = ", ".join(profile.used_by)
        already_present = _profile_already_configured(profile, config_parser, creds_parser)
        endpoint_ok = _profile_endpoint_matches(profile, config_parser)

        # ── Path 1: fully configured + endpoint matches → all good ─────
        if already_present and endpoint_ok:
            kind = "anonymous" if profile.anonymous else "credentialed"
            output_print(
                f"[bold green]✓[/bold green]  AWS profile [bold]{profile.name}[/bold] ({kind}) — {profile.description}"
            )
            continue

        # ── Path 2: present but endpoint mismatches → offer to repair ──
        if already_present and not endpoint_ok:
            section = _config_section_name(profile)
            current_endpoint = config_parser.get(section, "endpoint_url", fallback="").strip()
            shown = repr(current_endpoint) if current_endpoint else "<unset>"
            output_print(
                f"[bold yellow]⚠[/bold yellow]  AWS profile [bold]{profile.name}[/bold] is present but mis-configured"
            )
            output_print(f"       {profile.description}")
            output_print(f"       Expected endpoint_url: {profile.endpoint_url}")
            output_print(f"       Found:                 {shown}")

            if not interactive:
                output_print(
                    f"       Update {_AWS_CONFIG_PATH} manually, or re-run "
                    f"[bold]uv run atlantis setup[/bold] interactively to repair."
                )
                all_ok = False
                continue

            if not _prompt_yes(
                f"Repair '{profile.name}' to point at the ECMWF endpoint?",
                default=True,
            ):
                output_print("       [yellow]Skipped repair.[/yellow]")
                all_ok = False
                continue

            new_keys: tuple[str, str] | None = None
            if not profile.anonymous and _prompt_yes(
                f"Also replace the access/secret keys for '{profile.name}'?",
                default=False,
            ):
                new_keys = _prompt_credentials(profile.name, output_print)
                if new_keys is None:
                    # User aborted credential entry — abort the repair.
                    all_ok = False
                    continue

            access_key, secret_key = new_keys if new_keys else (None, None)
            _apply_profile_to_parsers(
                profile, config_parser, creds_parser, access_key=access_key, secret_key=secret_key
            )
            dirty_config = True
            if new_keys is not None:
                dirty_creds = True
            output_print(
                f"       [green]Repaired profile in {_AWS_CONFIG_PATH}"
                f"{' (and updated credentials)' if new_keys is not None else ' (existing credentials preserved)'}"
                f"[/green]"
            )
            continue

        # ── Path 3: missing entirely → create ──────────────────────────
        output_print(f"[bold yellow]⚠[/bold yellow]  AWS profile [bold]{profile.name}[/bold] not configured")
        output_print(f"       {profile.description}")
        output_print(f"       Used by: {used_by}")
        if profile.endpoint_url:
            output_print(f"       Endpoint: {profile.endpoint_url}")

        if not interactive:
            output_print(
                f"       Add the profile manually to {_AWS_CONFIG_PATH} (and "
                f"{_AWS_CREDENTIALS_PATH} when credentials are required)."
            )
            all_ok = False
            continue

        if profile.anonymous:
            _apply_profile_to_parsers(profile, config_parser, creds_parser, access_key=None, secret_key=None)
            dirty_config = True
            dirty_creds = True
            output_print(f"       [green]Added anonymous profile to {_AWS_CONFIG_PATH}[/green]")
            continue

        creds = _prompt_credentials(profile.name, output_print)
        if creds is None:
            all_ok = False
            continue

        access_key, secret_key = creds
        _apply_profile_to_parsers(profile, config_parser, creds_parser, access_key=access_key, secret_key=secret_key)
        dirty_config = True
        dirty_creds = True
        output_print(f"       [green]Saved profile to {_AWS_CONFIG_PATH} and {_AWS_CREDENTIALS_PATH}[/green]")

    if dirty_config:
        try:
            _write_ini(_AWS_CONFIG_PATH, config_parser, secret=False)
        except OSError as exc:
            output_print(f"       [red]Failed to write {_AWS_CONFIG_PATH}: {exc}[/red]")
            all_ok = False
    if dirty_creds:
        try:
            _write_ini(_AWS_CREDENTIALS_PATH, creds_parser, secret=True)
        except OSError as exc:
            output_print(f"       [red]Failed to write {_AWS_CREDENTIALS_PATH}: {exc}[/red]")
            all_ok = False

    return all_ok


# ── Main entry point ──────────────────────────────────────────────────────


def run_setup(
    *,
    auto_fix: bool = True,
    output: object | None = None,
    update_hashes: bool = False,
    interactive: bool | None = None,
) -> bool:
    """Bootstrap required data assets.

    Parameters
    ----------
    auto_fix:
        When *True* (the default) attempt to restore missing tracked
        files automatically via ``git restore``.
    output:
        An object with a ``print()`` method (e.g. ``rich.console.Console``).
        Falls back to the built-in ``print`` when *None*.
    update_hashes:
        When *True*, recompute SHA-256 hashes for all non-LFS assets
        and write them to ``config/asset_hashes.json``.
    interactive:
        When *True*, prompt for missing credentials (e.g. Earthdata token)
        and persist them to ``.env``. When *False*, only report what is
        missing. *None* (default) auto-detects: interactive if stdin is a TTY.

    Returns:
        *True* when all assets and credentials are present, *False* when
        something is still missing after attempts.
    """
    _print = output.print if output is not None else print
    if interactive is None:
        interactive = sys.stdin.isatty()
    # --check-only (auto_fix=False) must never modify the filesystem, so
    # disable interactive prompts that would write to .env or ~/.aws/.
    if not auto_fix:
        interactive = False

    _print("[bold]Asset check[/bold]\n")

    # ── Step 1: geo dependencies ────────────────────────────────────────────
    if _check_geo_dependency():
        _print("[bold green]✓[/bold green]  geo dependencies (geopandas, shapely)")
    else:
        _print("[bold yellow]⚠[/bold yellow]  geo dependencies not installed")
        _print("       Run: uv sync --extra geo\n")

    # GDAL HDF4 support (optional — only the MODIS laads_hdf4 backend needs it).
    if _check_hdf4_support():
        _print("[bold green]✓[/bold green]  GDAL HDF4 driver (needed by MODIS laads_hdf4 backend)")
    else:
        _print("[bold yellow]⚠[/bold yellow]  GDAL HDF4 driver not available")
        _print("       Required by: MODIS --modis-backend laads_hdf4 (historical archive 2003+)")
        _print("       Not required for: MODIS --modis-backend lance_geotiff (NRT, last ~1 week)")
        _print("       conda:  conda install -c conda-forge libgdal-hdf4")
        _print("       source: see docs/gdal-install.md\n")

    # ── Step 2: required assets ─────────────────────────────────────────────
    any_missing = False
    hashes: dict[str, str] = {} if update_hashes else {}

    for label, rel_path, tracked in ASSETS:
        abs_path = _REPO_ROOT / rel_path
        rel_str = str(rel_path)

        if abs_path.exists() and abs_path.stat().st_size > 0:
            # Check for LFS pointer files
            if _is_lfs_pointer(abs_path):
                _print(f"[bold yellow]⚠[/bold yellow]  [LFS-POINTER] {label} — {rel_path}")
                _print("       Run: git lfs pull -- {rel_path}")
                any_missing = True
                continue

            # ── Hash verification (non-LFS assets only) ─────────────────
            if rel_str in _HASHED_ASSETS:
                expected = _asset_expected_hash(rel_str)
                if update_hashes:
                    actual_hash = _compute_sha256(abs_path)
                    hashes[rel_str] = f"sha256:{actual_hash}"
                    _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
                    _print(f"       sha256:{actual_hash}")
                elif expected is not None:
                    actual_hash = _compute_sha256(abs_path)
                    expected_hash = expected.removeprefix("sha256:")
                    if actual_hash != expected_hash:
                        _print(f"[bold yellow]⚠[/bold yellow]  [CHANGED] {label} — {rel_path}")
                        _print(f"       expected: {expected}")
                        _print(f"       actual:   sha256:{actual_hash}")
                        if auto_fix:
                            restored = _git_restore(abs_path)
                            if restored and abs_path.exists():
                                new_hash = _compute_sha256(abs_path)
                                if new_hash == expected_hash:
                                    _print("       [green]Restored from git — version matches[/green]")
                                else:
                                    _print(
                                        "       [yellow]Restored from git —"
                                        " but hash differs (may be intentional)[/yellow]"
                                    )
                            else:
                                _print("       git restore failed. Manual restore:")
                                _print(f"       git checkout HEAD -- {rel_path}")
                        any_missing = True
                    else:
                        _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
                else:
                    _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
            else:
                _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
            continue

        # File missing or empty
        _print(f"[bold red]✗[/bold red]  [MISSING] {label} — {rel_path}")

        if not tracked:
            _print(f"       File must be provided manually: {rel_path}")
            any_missing = True
            continue

        if auto_fix:
            restored = _git_restore(abs_path)
            if restored and abs_path.exists() and abs_path.stat().st_size > 0:
                _print("       [green]Restored from git[/green]")
            else:
                _print("       git restore failed. Manual restore:")
                _print(f"       git checkout HEAD -- {rel_path}")
                any_missing = True
        else:
            _print(f"       Restore with: git checkout HEAD — {rel_path}")
            any_missing = True

    # ── Update hashes ───────────────────────────────────────────────────────
    if update_hashes and hashes:
        _write_asset_hashes(hashes)
        _print(f"\n[bold]Wrote updated hashes → {_ASSET_HASHES_PATH.relative_to(_REPO_ROOT)}[/bold]")

    # ── Step 3: credentials ─────────────────────────────────────────────────
    _print("\n[bold]Credentials check[/bold]\n")
    creds_ok = _check_credentials(interactive=interactive, output_print=_print)

    # ── Step 4: AWS profiles ────────────────────────────────────────────────
    _print("\n[bold]AWS profile check[/bold]\n")
    aws_ok = _check_aws_profiles(interactive=interactive, output_print=_print)

    # ── Summary ──────────────────────────────────────────────────────────────
    _print("")
    if any_missing:
        _print("[bold yellow]⚠[/bold yellow]  Some assets are missing or out of date.  See messages above.")
        return False
    if not creds_ok:
        _print("[bold yellow]⚠[/bold yellow]  Some credentials are not configured.  See messages above.")
        return False
    if not aws_ok:
        _print("[bold yellow]⚠[/bold yellow]  Some AWS profiles are not configured.  See messages above.")
        return False

    _print("[bold green]✓[/bold green]  All data assets, credentials, and AWS profiles are present and up-to-date.")
    return True


# ── AWS profile live-verification ─────────────────────────────────────────


def verify_aws_profile(profile: AwsProfile, *, timeout: int = 10) -> tuple[bool, str]:
    """Perform a single round-trip S3 call to verify a profile actually works.

    For credentialed profiles, lists ``s3://atlantis/reference/`` (the
    canonical ECMWF object-store path). For anonymous profiles, lists
    ``s3://noaa-jpss/`` via ``signature_version=UNSIGNED`` (the AWS
    convention for unsigned access — the profile alone is not enough,
    callers must pass the explicit Config override too).

    Returns ``(ok, message)``. *message* never contains credentials.
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError as exc:
        return False, f"boto3 not installed ({exc})"

    try:
        session = boto3.Session(profile_name=profile.name)
    except Exception as exc:  # ProfileNotFound, etc.
        return False, f"profile lookup failed: {exc}"

    client_kwargs: dict = {"config": Config(connect_timeout=timeout, read_timeout=timeout)}
    if profile.anonymous:
        # Override signing — the s3=signature_version=unsigned block in
        # ~/.aws/config is not honoured by boto3, only by the AWS CLI flag.
        client_kwargs["config"] = Config(
            signature_version=UNSIGNED,
            connect_timeout=timeout,
            read_timeout=timeout,
        )

    try:
        s3 = session.client("s3", **client_kwargs)
    except Exception as exc:
        return False, f"client construction failed: {exc}"

    bucket, prefix = ("noaa-jpss", "") if profile.anonymous else ("atlantis", "reference/")
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1, Delimiter="/")
    except NoCredentialsError:
        return False, "no credentials available"
    except (ClientError, BotoCoreError) as exc:
        return False, f"{type(exc).__name__}: {exc}"

    n_objects = resp.get("KeyCount", 0)
    n_prefixes = len(resp.get("CommonPrefixes", []))
    return True, f"s3://{bucket}/{prefix} reachable ({n_objects} object(s), {n_prefixes} prefix(es))"


def verify_aws_profiles(*, output_print=print, timeout: int = 10) -> bool:
    """Run :func:`verify_aws_profile` on every registered profile.

    Used by the ``atlantis setup --verify`` flow to confirm that profiles
    are not just present in the INI files but actually reach their target
    S3 endpoint.
    """
    output_print("\n[bold]AWS profile live verification[/bold]\n")
    all_ok = True
    for profile in AWS_PROFILES:
        ok_, msg = verify_aws_profile(profile, timeout=timeout)
        marker = "[bold green]✓[/bold green]" if ok_ else "[bold red]✗[/bold red]"
        output_print(f"{marker}  AWS profile [bold]{profile.name}[/bold] — {msg}")
        if not ok_:
            all_ok = False
    return all_ok


def get_missing_assets() -> list[str]:
    """Return a list of labels for assets that are missing.

    Useful for quick pre-flight checks before other commands.
    """
    missing: list[str] = []
    for label, rel_path, _tracked in ASSETS:
        abs_path = _REPO_ROOT / rel_path
        if not abs_path.exists() or abs_path.stat().st_size == 0:
            missing.append(label)
        elif _is_lfs_pointer(abs_path):
            missing.append(f"{label} (LFS pointer, not pulled)")
        elif str(rel_path) in _HASHED_ASSETS:
            expected = _asset_expected_hash(str(rel_path))
            if expected is not None:
                actual_hash = _compute_sha256(abs_path)
                expected_hash = expected.removeprefix("sha256:")
                if actual_hash != expected_hash:
                    missing.append(f"{label} (hash mismatch)")
    return missing
