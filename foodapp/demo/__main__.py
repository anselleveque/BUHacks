"""Backward-compatible alias for python3 -m foodapp.server --demo."""

from foodapp import server


def main(argv=None):
    return server.main(argv, demo_alias=True)


if __name__ == "__main__":
    main()
