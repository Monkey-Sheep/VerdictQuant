"""PyInstaller entry point for the standalone VerdictQuant updater."""

from pa_agent.update.installer import main

if __name__ == "__main__":
    raise SystemExit(main())
