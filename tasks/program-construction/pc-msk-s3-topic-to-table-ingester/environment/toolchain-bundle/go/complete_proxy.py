"""Make the offline Go proxy serve any subset of the bundle.

`go mod download all` fetches source only for the versions the bundle selects. A program that imports
fewer packages can select a lower version: the highest one that a bundled module's go.mod requires.
Fetch those versions too, then list only versions with source so `go get` never picks one it cannot read.

complete_proxy.py <proxy dir> <file of "path version" lines from `go list -m all`>
"""
import pathlib, re, subprocess, sys

root = pathlib.Path(sys.argv[1])
selected = [line.split() for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if len(line.split()) == 2]
bundled = {path for path, _ in selected}
escape = lambda part: re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), part)
REQUIRE = re.compile(r"^(?:require\s+)?(\S+)\s+(v\S+)$")


def requirements(mod):
    block, found = False, set()
    for line in mod.read_text(errors="ignore").splitlines():
        line = line.split("//")[0].strip()
        if line.startswith("require ("):
            block = True
        elif block and line == ")":
            block = False
        elif (block or line.startswith("require ")) and (m := REQUIRE.match(line)):
            found.add(m.groups())
    return found


wanted = set()
for path, version in selected:
    mod = root / escape(path) / "@v" / f"{escape(version)}.mod"
    if mod.exists():
        wanted |= {(p, v) for p, v in requirements(mod) if p in bundled}
missing = sorted(f"{p}@{v}" for p, v in wanted if not (root / escape(p) / "@v" / f"{escape(v)}.zip").exists())
for start in range(0, len(missing), 100):
    subprocess.run(["go", "mod", "download", *missing[start:start + 100]], check=True, stdout=subprocess.DEVNULL)
for listing in root.rglob("@v/list"):
    listing.write_text("".join(f"{p.name[:-4]}\n" for p in sorted(listing.parent.glob("*.zip"))))
print(f"fetched {len(missing)} more versions")
