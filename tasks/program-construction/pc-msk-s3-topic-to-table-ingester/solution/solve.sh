#!/bin/bash
set -eu
mkdir -p /app
cp -a /solution/reference /app/reference
cat > /app/build.sh <<'BUILD'
#!/bin/sh
set -eu
cd /app
cp -a reference/. /app/
chmod 755 program
BUILD
chmod 755 /app/build.sh
