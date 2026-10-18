"""Start the local presales assistant with persistent SQLite storage."""

import argparse
import os
from pathlib import Path
import socket

import uvicorn


ROOT = Path(__file__).resolve().parent


def project_path(path: Path) -> Path:
    return (path if path.is_absolute() else ROOT / path).resolve()


def configure(database: Path, port: int):
    database = project_path(database)
    if database.suffix not in {".sqlite3", ".db"}:
        raise ValueError("请选择持久 SQLite 文件（.sqlite3 或 .db）。")
    if not 1024 <= port <= 65535:
        raise ValueError("端口必须在 1024 至 65535 之间。")
    # Refuse occupied ports before importing the app or initializing storage.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    os.environ["SAAS_DATABASE_URL"] = "sqlite:///" + database.as_posix()
    from app.config import get_settings
    get_settings.cache_clear()
    return database


def validate_ui_directory(ui_dir: Path):
    ui_dir = project_path(ui_dir)
    if not (ui_dir / "index.html").is_file() or not (ui_dir / "assets").is_dir():
        raise ValueError("UI目录必须包含正式构建的 index.html 和 assets 目录。")
    return ui_dir


def run(database: Path, port: int, ui_dir: Path | None = None):
    # Validate an explicit delivery bundle before configuring or initializing
    # the chosen persistent database. Default startup keeps its existing UI.
    if ui_dir is not None:
        ui_dir = validate_ui_directory(ui_dir)
    database = configure(database, port)
    print(f"页面：http://127.0.0.1:{port}/\n持久数据：{database}\n"
          "服务退出不会清理聊天或草案；重启使用相同命令。\n"
          "当前需求和未提交方案只在本进程中保留，重启后需重新评估。",
          flush=True)
    application = "app.main:app"
    if ui_dir is not None:
        import app.main as main
        from fastapi.staticfiles import StaticFiles
        main.UI_DIR = ui_dir
        for route in main.app.routes:
            if getattr(route, "path", None) == "/assets":
                route.app = StaticFiles(directory=ui_dir / "assets")
        application = main.app
        print(f"正式界面构建：{ui_dir}", flush=True)
    uvicorn.run(application, host="127.0.0.1", port=port,
                log_level="info", workers=1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path,
                        help="SQLite 文件；省略时读取 SAAS_DATABASE_URL，默认 data/saas_assistant.db。")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ui-dir", type=Path, help="界面构建目录；默认 app/ui。")
    args = parser.parse_args(argv)

    # Keep .env, SQLite paths and imported application settings rooted here,
    # including when the launcher is invoked from another working directory.
    os.chdir(ROOT)
    if args.database is None:
        from app.config import get_settings
        from app.db.session import database_path_from_url
        get_settings.cache_clear()
        database = database_path_from_url(get_settings().saas_database_url)
    else:
        database = args.database
    run(database, args.port, args.ui_dir)


if __name__ == "__main__":
    main()
