# Anonymisation policy

Prism is developed against a real estate and published openly. Nothing in this
repository may name that estate.

`tools/check_anonymised.py` enforces this, and the pre-push hook runs it. A
push that would publish a real identifier fails before it leaves the machine.

## What must never appear

- **Hostnames** — real machine names, in code, comments, docstrings, test
  fixtures and planning docs alike.
- **The AD domain**, in any form: DNS suffix, NetBIOS name, or LDAP DN
  components.
- **Accounts** — service accounts and personal accounts. A real AD username
  survived in a test fixture for months because nothing could derive it.
- **Addresses** — any RFC 1918 address that is not a documentation
  placeholder.

Author attribution in `LICENSE` and `README.md` is deliberate and exempt. The
point is to protect the estate, not the maintainer's byline.

## What to write instead

A fictional fleet, role-named so the docs stay readable:

| role | use |
|---|---|
| Domain controllers | `DC01`, `DC02`, `DC03` |
| File server | `FILE01` |
| Application servers | `APP01`, `APP02` |
| Database | `SQL01` |
| Mail | `MAIL01` |
| Management | `MGT01` |
| Lab / test | `LAB01` |
| Non-domain-joined host | `STANDALONE01` |

Domain: `ad.example.com`, NetBIOS `EXAMPLE`, DN `DC=EXAMPLE,DC=COM`.
Accounts: `a.admin`, `svc`.
Addresses: RFC 5737 documentation ranges — `192.0.2.0/24`, `198.51.100.0/24`,
`203.0.113.0/24`. The `10.0.0.x` and `192.168.1.x` fixtures already in the
suite are also accepted.

**There is deliberately no real-to-fictional mapping table in this file.**
Publishing one would publish the hostnames it exists to remove. Keep any
mapping you need locally.

## How the check knows what is secret

A committed deny-list of forbidden strings would leak every one of them to the
first reader. So the real values are never committed. They come from two
sources at check time:

1. **`config.json`** — gitignored, holds the deployment's real server names,
   hosts, AD domain and service account. Every distinctive token in it becomes
   a forbidden term.
2. **`.anonymisation-denylist`** — gitignored, optional, one term per line.
   For identifiers the config cannot supply: personal accounts, colleagues'
   names, internal codenames.

Only *shapes* are committed — the estate's hostname convention, an LDAP DN
naming a non-example domain, private addresses outside the documentation
ranges.

That split has a useful consequence: CI has no `config.json`, so it runs the
shape rules alone and stays green, while a developer's machine runs the full
check. The hook passes `--require-config` so a missing config **fails the
push** rather than quietly downgrading to the weaker ruleset.

## Running it

```bash
python tools/check_anonymised.py                  # every tracked and new file
python tools/check_anonymised.py --files a.py     # just these
python tools/check_anonymised.py --require-config # what the hook runs
```

Install the hook (once per clone):

```bash
python tools/install_hooks.py
```

## If the check fires on something legitimate

Do not add the real value to an allowlist — that defeats the purpose. Either
rewrite the text to use the fictional fleet, or, if a *shape* rule is genuinely
over-matching, widen the shape in `tools/check_anonymised.py` and say why in a
comment.
