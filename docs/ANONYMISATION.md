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

Only **generic** shapes are committed — an LDAP DN naming a non-example domain,
and private addresses outside the documentation ranges. Neither says anything
about a particular organisation.

**The estate's own hostname convention is not committed either.** It lives in
the deny-list as a `regex:` line. A committed pattern such as
`<PREFIX>[A-Z]{2,4}\d{2}` is a smaller disclosure than a hostname, but it still
tells any reader of a public repo how the organisation names its machines, and
the mechanism to hold it already existed. The consequence is worth stating: with
no deny-list, there is no hostname rule at all — so the warning CI emits when
the secret is missing is load-bearing, not cosmetic.

The checker's own source and its test file are **not skipped**. Only the regex
rules are suppressed for them, because they have to contain the shapes they
define; config-derived terms and the address rule still apply. The earlier
blanket exemption was a hole, and an occupied one — the two files the checker
refused to look at turned out to be carrying a real hostname and a real IP.
Where a test genuinely needs an address the rule should fire on, it assembles
it at runtime rather than writing a literal.

That split has a useful consequence: CI has no `config.json`, so it runs the
shape rules alone and stays green, while a developer's machine runs the full
check. The hook passes `--require-config` so a missing config **fails the
push** rather than quietly downgrading to the weaker ruleset.

## Four layers, because one was not enough

This repository leaked real hostnames **twice**, and both times a human had
already declared it clean. A single check that someone has to remember to run
is not a control. There are now four, each covering a way the previous one
fails.

| # | Layer | Catches | Fails when |
|---|---|---|---|
| 1 | `pre-commit` hook | a leak before it ever enters a commit | hooks not installed |
| 2 | `pre-push` hook | the whole range being pushed — every commit's tree **and** every commit message | hooks not installed, or `--no-verify` |
| 3 | **CI** | the same, on GitHub, on every push and PR | never — this is the backstop |
| 4 | Branch protection | a push that skipped CI | only if you enable it (see below) |

Layer 2 scans the **range**, not the working tree, because a clean tip says
nothing about the commits underneath it — three commits went out on 2026-08-06
carrying a file whose cleanup only landed in the fourth. And it scans commit
**messages**, because on 2026-08-03 a hostname reached GitHub inside one while
every file was clean.

CI passes `--redact`, which masks matched values. A build log on a public repo
is public, and GitHub masks a secret's exact full value — not the individual
lines inside a multi-line one.

## Running it

```bash
python tools/check_anonymised.py                        # working tree
python tools/check_anonymised.py --files a.py b.md      # just these
python tools/check_anonymised.py --range origin/master..HEAD   # commits + messages
python tools/check_anonymised.py --redact               # mask matches (CI)
```

Enable the hooks (once per clone):

```bash
python tools/install_hooks.py
```

That sets `core.hooksPath` to the tracked `.githooks/` directory rather than
copying files into `.git/hooks`, so the hooks are always whatever the
repository says they are. Verify with `python tools/install_hooks.py --check`.

## Repo-admin configuration (done — recorded here so it can be re-created)

Both of the following are configured on `Rinkatecam/Prism`. If the repo is ever
recreated, they have to be set up again — the whole gate is only as strong as
layer 4.

Note that the **ruleset** is also named `ANONYMISATION_DENYLIST`, which is the
same name as the **Actions secret**. They are two unrelated things that happen
to share a name; do not assume configuring one configures the other.


**1. Add the `ANONYMISATION_DENYLIST` secret** — Settings → Secrets and
variables → Actions. Generate the value with:

```bash
python tools/check_anonymised.py --emit-denylist
```

That merges the config-derived terms with `.anonymisation-denylist` into the
exact set this machine checks against — CI has no `config.json`, so the secret
has to carry both. **It prints real values; never run it in CI or anywhere the
output is captured.**

Without the secret, CI runs shape rules alone and cannot recognise the AD
domain or an account name; it emits a warning on every run saying so. The shape
rules still catch anything matching the estate's hostname convention, so a
newly-added server is covered even if the secret is stale — regenerate it when
the domain or the accounts change, which is rare.

**2. Enable branch protection on `master`** — Settings → Branches → require the
`Anonymisation (no real hostnames)` status check to pass. Until this is on, a
direct push with `--no-verify` reaches GitHub and CI only tells you afterwards.
This is the only layer that makes "never" literally true.

## If the check fires on something legitimate

Do not add the real value to an allowlist — that defeats the purpose. Either
rewrite the text to use the fictional fleet, or, if a *shape* rule is genuinely
over-matching, widen the shape in `tools/check_anonymised.py` and say why in a
comment.
