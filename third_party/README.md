# Distributed licence materials

- `frontend/`: complete notices from official releases of vendored frontend components, plus their dependency notice supplement. Its manifest records origins, versions and hashes. Existing bundle notices and font licence files remain alongside the assets.
- `python/`: official complete licence/third-party notice supplements for wheels that omit them. Its manifest records which installed version the supplement covers, its source and content hashes.
- `native/`: original full notices from exact source-release Cargo lock graphs of Rust wheels, including optional/build/platform dependencies, plus the verified embedded OpenSSL version. Multiple-license selections are recorded. colored (MPL-2.0) includes its corresponding source. The supplement does not claim every graph component is linked.
- `runtime/`: actual Python and Debian image inventory, copied package/native notices and common licence texts. Rebuilt inside each Docker image by `scripts/collect_runtime_licenses.py`. Original package and OS notice files are retained too. The checked-in snapshot documents the audited Linux image; another architecture/base image can have different OS components.
- `sources/`: corresponding unmodified release sources of the two weak-copyleft Python components. Official source URLs and SHA-256 hashes are in its manifest. These files can be obtained with the public repository source archive, without access to any private deployment.

Each third-party component remains under its own licence. Klado's Apache-2.0 licence does not replace those licences or restrict the recipient's rights to modify or replace an LGPL/MPL component. The component notices, complete texts and source references control; classification labels describe project review policy only.
