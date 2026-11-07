"""Entry point of schauling-service.exe: the engine without a window, for the SC-overlay window.

    schauling-service.exe --port P --token T --parent-pid N
"""
import sys

from schauling.__main__ import main

if __name__ == "__main__":
    main(["--service"] + sys.argv[1:])
