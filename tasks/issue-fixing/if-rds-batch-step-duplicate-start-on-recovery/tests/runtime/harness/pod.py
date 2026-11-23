"""Container entry adapter. Cold image startup precedes the worker entrypoint."""
import os
import sys
import time
from .step_body import announce


def main():
    announce('pod_boot', step=os.environ['DAGSTER_STEP_KEY'],
             attempt=int(os.environ['DAGSTER_STEP_ATTEMPT']))
    time.sleep(float(os.environ.get('POD_BOOT_DELAY', '0')))
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == '__main__':
    main()
