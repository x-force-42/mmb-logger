"""Entrypoint: `python -m mmb_logger`."""

from mmb_logger.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
