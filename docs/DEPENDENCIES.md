# Dependencies

Prism uses a **hash-pinned, fully-resolved lockfile** for production installs. This document is the operator-facing reference.

## TL;DR

- **Production install:** `pip install --require-hashes -r requirements.lock`
- **Never** run `pip install -r requirements.txt` on a production host.
- **Updating:** edit `requirements.txt`, run `pip-compile --generate-hashes`, commit both files.

## Why two files?

| File | Role |
| --- | --- |
| `requirements.txt` | Human-readable source of truth. Lists the direct dependencies Prism imports, with range pins (`flask==3.1.*`, `cryptography>=42.0,<44.0`, etc.). Edit this file. |
| `requirements.lock` | Generated artifact. Pins every package — direct AND transitive — to a single version, with SHA-256 hashes for every wheel/sdist that can satisfy that version. Do not edit by hand. |

The lockfile is what guarantees that two operators installing Prism on different days, from different network paths, end up with byte-identical Python code on disk. The range-pinned `requirements.txt` alone does not — it lets pip pick whatever satisfies the range *at install time*.

## Production install

On the deployment host:

```bash
python -m venv .venv
source .venv/bin/activate     # or .venv\Scripts\activate on Windows
pip install --require-hashes -r requirements.lock
```

`--require-hashes` tells pip to refuse to install any package whose downloaded artifact does not match one of the hashes listed in the lockfile. A tampered mirror, a typosquatted package, or a wheel substitution mid-flight all fail loudly here.

## Updating dependencies

1. Edit `requirements.txt` — bump a range, add or remove a direct dependency.
2. Regenerate the lockfile:

   ```bash
   pip install pip-tools          # one-time, dev tool
   pip-compile --generate-hashes --strip-extras --no-emit-trusted-host \
       --output-file=requirements.lock requirements.txt
   ```

3. Verify the new lockfile installs cleanly in a fresh venv:

   ```bash
   python -m venv .venv-verify
   source .venv-verify/bin/activate
   pip install --require-hashes -r requirements.lock
   python -c "import app; print('OK')"
   deactivate && rm -rf .venv-verify
   ```

4. Commit `requirements.txt` AND `requirements.lock` together in the same commit. CI will refuse to merge if the lock is out of sync with the requirements (the hash-checked install will fail to resolve).

The `--strip-extras` flag is important: pip-compile otherwise records optional-extra markers that confuse `--require-hashes`. The `--no-emit-trusted-host` flag prevents Windows-side pip configurations from baking `--trusted-host` directives into the lockfile, which would weaken the integrity guarantee.

## Known supply-chain risks

The lockfile defends against:

- **Tampered mirrors** — a malicious PyPI mirror substituting a modified wheel cannot pass hash verification.
- **Typosquats landing transitively** — a typoed package name in a sub-dependency cannot be silently introduced without changing the lockfile and showing up in code review.
- **Version drift** — every operator gets the exact same closure, so "works on my box" supply-chain divergence is eliminated.

The lockfile does **NOT** defend against:

- **Maintainer-credential compromise of an upstream package** — if `cryptography`'s maintainer account is phished and a new release is published, our next `pip-compile` will pick it up and lock its hash. The hash will match the malicious wheel because the malicious wheel is what PyPI is serving. This is a separate problem; mitigations live elsewhere (Sigstore-verified releases, vendoring critical packages, manual review of every version bump for tier-0 dependencies). Treat every `pip-compile` run as a security-relevant code change and review the diff.
- **Compromise of pip-tools or pip themselves** — this is the build-time toolchain risk. Run `pip-compile` only on a trusted workstation.

## Air-gapped sites / internal proxy / Artifactory

If your install path goes through an internal proxy (Artifactory, Nexus, devpi, a private PyPI mirror), do **not** rely on the proxy's name-based allowlist as your integrity boundary. Configure the proxy to mirror only the **exact `name == version` tuples** present in `requirements.lock`, and ideally pin by the wheel's SHA-256 as well.

A "name-only" allowlist at the proxy is no defence: a compromised upstream that publishes a patch release reaches you on the next sync. The lockfile's hashes are what catch this — but only if the proxy actually serves the bytes the lockfile expects. Verify periodically by re-running `pip install --require-hashes` against the proxy and confirming a clean install.

## CI enforcement

`.github/workflows/ci.yml` runs `pip install --require-hashes -r requirements.lock` on every push and pull request. Any drift between `requirements.txt` and `requirements.lock` shows up there before merge. A separate audit job runs `pip-audit` against the lockfile and prints findings (it does not yet fail the build — that is a Sprint-3 hardening item).
