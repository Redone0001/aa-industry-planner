# Release workflow

For completed code changes, release to GitHub by default. Honor any request to keep a change local or hold a release.

1. Verify the changes and bump the package version in `pyproject.toml` and `aa_industry_planner/__init__.py`. Update the version-pinned install commands in `README.md`.
2. Commit the changes, create an annotated `v<version>` tag, and push the tracked branch and tag. Verify that GitHub serves the new tag.
3. Include the pip upgrade command pinned to the new tag in the handoff. Give the Alliance Auth `python manage.py migrate aa_industry_planner` and `python manage.py collectstatic --noinput` commands and note which services to restart.
