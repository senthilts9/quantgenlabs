# Vendored package

Source: https://github.com/senthilts9/cryptodesk (private)
Commit: 8023e05a
Vendored: 2026-09-14T04:20:00Z

Vendored rather than pip-installed because the upstream repo is private, and
threading a token into the Docker build to reach it would put a credential in
the image layer history. The package is standard-library only, so a copy costs
nothing at build time and adds no dependency to requirements.txt.

Re-sync with:

    rsync -a --exclude='__pycache__' \
      ../../crypto-trading-desk/cryptodesk/ ./

The commit above is what this copy was taken from; compare it against upstream
`git rev-parse HEAD` to detect drift.
