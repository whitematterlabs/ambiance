# PAI

personal artificial intelligence

## Build PAI.app

From the repo root:

```sh
./paibuild
```

`paibuild` is repo-only developer tooling. It produces `macos/build/PAI.app`,
defaults to Release, incrementally rebuilds Swift/web/Python/seed/signing layers
as needed, and is never installed into `~/.pai`.
