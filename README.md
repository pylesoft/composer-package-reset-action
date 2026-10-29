# Restore Pyle development dependencies

Reset every present Pyle core dependency to its owning repository's default branch, regenerate the matching Composer lock, and push both files in one commit.

```yaml
- uses: actions/checkout@v6
  with:
    ref: ${{ github.event.repository.default_branch }}
    token: ${{ secrets.GH_ACTION_SPLIT_PACKAGES_TOKEN }}

# Set up PHP/Composer and Composer authentication before running the action.
- uses: pylesoft/composer-package-reset-action@<reviewed-commit>
  with:
    development-branch: ${{ github.event.repository.default_branch }}
    github-token: ${{ secrets.GH_ACTION_SPLIT_PACKAGES_TOKEN }}
```

The caller must check out a clean, current development branch and provide Composer and Git push credentials. Run this workflow after normal publication and production synchronization, not during draft preparation or hotfixes.

The core registry covers `floorbox/cbox-framework`, `floorbox/cbox-admin-module`, `floorbox/cbox-storefront-module`, `pylesoft/admin`, and `floorbox/pyle-erp-bridge-module`. Only present requirements are changed; `require-dev` entries stay in their section. Feature branches, aliases, commit pins, and release constraints are replaced deliberately.

Composer updates are restricted to those present packages. The existing lock is preserved, unrelated dependency changes fail validation, and no broad update fallback runs. Missing locks, failed metadata lookups, solver conflicts, unexpected files, or rejected normal pushes fail visibly. Correct constraints still refresh stale lock entries. An unchanged result does not create a commit.

For multiple manifests, all constraints are restored before Composer resolves either lock. A core package selected by one manifest is also refreshed in other selected locks where it already exists. This keeps Pyle's local Framework dependency metadata and its shared ERP Bridge lock entries consistent without adding root requirements.

Inputs include `composer-files` (default `["composer.json"]`), `package-repositories` (owning-repository overrides for the dummy sandbox), `commit-message`, and `push`. Setting `push: 'false'` leaves validated edits without committing. Outputs are `outcome`, `commit-sha`, and `changed-files`; the action also writes a job summary.

Run the integration tests with `python3 -m unittest discover -s tests -v`. They use real Composer and temporary Git repositories; only GitHub metadata calls are replaced with local fixture responses.
