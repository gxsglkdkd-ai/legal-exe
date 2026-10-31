import asyncio
import shlex
import shutil
import signal
import sys
from pathlib import Path

from core.config import Config
from core.device_registry import DeviceRegistry
from core.rcon_client import MinecraftRcon
from core.web_server import WebServer
from core.input_injector import InputInjector
from utils.console import console
from utils.device_info import DeviceInfo
from utils.paths import resource_path
from commands.mode import ModeManager


class MinecraftRemoteChat:
    def __init__(self):
        self.config: Config | None = None
        self.registry: DeviceRegistry | None = None
        self.rcon: MinecraftRcon | None = None
        self.web_server: WebServer | None = None
        self.injector: InputInjector | None = None
        self.mode_manager: ModeManager | None = None
        self.config_path: Path = Path("config.yaml")
        self._running = False

    async def initialize(self) -> bool:
        # В onefile-сборке config.yaml лежит в _MEIPASS. Копируем рядом с exe/скриптом,
        # чтобы mode можно было сохранять.
        self.config_path = Path("config.yaml")
        if not self.config_path.exists():
            bundled = resource_path("config.yaml")
            try:
                if bundled.exists() and bundled.resolve() != self.config_path.resolve():
                    shutil.copy2(bundled, self.config_path)
            except Exception:
                pass

        if not self.config_path.exists():
            console.error("config.yaml не найден! Создайте его из примера.")
            return False

        self.config = Config.load(str(self.config_path))
        console.banner(self.config.app.name, self.config.app.version)
        console.info(f"Режим работы: {self.config.mode.upper()}")

        self.registry = DeviceRegistry(
            self.config.device_registry.file,
            self.config.device_registry.auto_register_host
        )
        await self.registry.load()

        self.rcon = MinecraftRcon(self.config.rcon)
        if self.config.mode == "rcon":
            await self.rcon.connect()

        self.injector = InputInjector(self.config.local_input)

        self.web_server = WebServer(
            self.config.web_server,
            self.registry,
            self.rcon,
            self.injector,
            mode_provider=lambda: self.config.mode,
            static_dir=resource_path("static")
        )
        await self.web_server.start()

        self.mode_manager = ModeManager(self)

        host_info = DeviceInfo.detect()
        console.info(f"Устройство: {host_info.display_name()}")
        console.info(f"ID: {host_info.short_id()} | IP: {host_info.ip}")

        if self.registry.host_device:
            console.success(
                f"Хост зарегистрирован: "
                f"{self.registry.host_device.name} ({self.registry.host_device.id})"
            )

        return True

    async def run_console(self):
        self._running = True
        console.help()

        while self._running:
            try:
                cmd = await asyncio.to_thread(console.input, ">")
                if not cmd:
                    continue
                await self.process_command(cmd)
            except (KeyboardInterrupt, EOFError):
                break
            except Exception as e:
                console.error(f"Ошибка: {e}")

        await self.shutdown()

    async def process_command(self, cmd: str):
        raw = cmd.strip()
        if not raw:
            return

        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        argstr = parts[1] if len(parts) > 1 else ""

        if command in ("exit", "quit"):
            self._running = False
            return

        if command == "help":
            console.help()

        elif command == "status":
            await self.cmd_status()

        elif command == "reg" and argstr.strip().lower() == "list":
            await self.cmd_reg_list()

        elif command == "register":
            await self.cmd_register(shlex.split(argstr))

        elif command == "send":
            await self.cmd_send(argstr.split(maxsplit=1))

        elif command == "chat":
            await self.cmd_chat(argstr)

        elif command == "rcon":
            await self.cmd_rcon(argstr)

        elif command == "mode":
            await self.mode_manager.cmd_mode(shlex.split(argstr))

        elif command == "cleanup":
            await self.cmd_cleanup(shlex.split(argstr))

        else:
            console.warning(f"Неизвестная команда: {command}. Введите 'help'.")

    async def cmd_status(self):
        devices = await self.registry.list_devices()
        mode = self.config.mode.upper()
        rcon_status = "🟢" if (self.rcon and self.rcon.connected) else "🔴"
        window = None
        if self.injector:
            window = await asyncio.to_thread(self.injector.find_minecraft_window)
        injector_status = "🟢" if window else "🔴"

        console.info(
            f"Режим: {mode} | Устройств: {len(devices)} | "
            f"RCON: {rcon_status} | Окно MC: {injector_status}"
        )
        console.info(
            f"Веб-сервер: http://{self.config.web_server.host}:"
            f"{self.config.web_server.port}"
        )

    async def cmd_reg_list(self):
        devices = await self.registry.list_devices()
        if not devices:
            console.warning("Нет зарегистрированных устройств")
            return

        print(
            f"\n{console.Fore.CYAN}"
            f"{'ID':<10} {'Имя':<25} {'Тип':<10} {'IP':<15} {'Статус'}"
            f"{console.Style.RESET_ALL}"
        )
        print(f"{console.Fore.CYAN}{'─' * 70}{console.Style.RESET_ALL}")
        for d in devices:
            status = (
                f"{console.Fore.GREEN}●{console.Style.RESET_ALL}"
                if d.is_active
                else f"{console.Fore.RED}○{console.Style.RESET_ALL}"
            )
            host_mark = (
                " 🏠"
                if self.registry.host_device and d.id == self.registry.host_device.id
                else ""
            )
            print(
                f"{d.id:<10} {d.name:<25} {d.type:<10} "
                f"{d.ip:<15} {status}{host_mark}"
            )
        print()

    async def cmd_register(self, args: list[str]):
        if not self.registry.host_device:
            console.error("Хост не определен")
            return

        name = args[0] if args else self.registry.host_device.name
        device_type = args[1] if len(args) > 1 else "PC"

        self.registry.host_device.name = name
        self.registry.host_device.type = device_type
        await self.registry.register(self.registry.host_device)
        console.success(f"Хост зарегистрирован: {name} ({device_type})")

    async def cmd_send(self, args: list[str]):
        if len(args) < 2:
            console.error("Использование: send <device_id> <message>")
            return

        device_id = args[0].upper()
        message = args[1]

        device = await self.registry.get(device_id)
        if not device:
            console.error(f"Устройство {device_id} не найдено")
            return

        result = await self.mode_manager.send_as(device, message)
        if result.get("success"):
            console.success(
                f"Отправлено от {device.name}: {result.get('detail', 'OK')}"
            )
        else:
            console.error(f"Ошибка: {result.get('error')}")

    async def cmd_chat(self, args: str):
        if not args.strip():
            console.error("Использование: chat <message>")
            return

        if not self.registry.host_device:
            console.error("Хост не зарегистрирован. Сначала: register <name>")
            return

        result = await self.mode_manager.send_as(
            self.registry.host_device, args.strip()
        )
        if result.get("success"):
            console.success(f"Чат: {result.get('detail', 'OK')}")
        else:
            console.error(f"Ошибка: {result.get('error')}")

    async def cmd_rcon(self, args: str):
        if not args.strip():
            console.error("Использование: rcon <command>")
            return
        if not self.rcon:
            console.error("RCON не инициализирован")
            return

        response = await self.rcon.send_raw(args.strip())
        console.info(f"RCON: {response}")

    async def cmd_cleanup(self, args: list[str]):
        days = (
            int(args[0])
            if args
            else self.config.device_registry.cleanup_inactive_days
        )
        await self.registry.cleanup_inactive(days)
        console.success(f"Очистка завершена (неактивные > {days} дней)")

    async def shutdown(self):
        console.info("Завершение работы...")
        self._running = False
        if self.rcon:
            self.rcon.disconnect()
        if self.web_server:
            await self.web_server.stop()
        console.success("Готово!")


async def main():
    app = MinecraftRemoteChat()

    if not await app.initialize():
        sys.exit(1)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(app.shutdown())
            )
        except NotImplementedError:
            pass

    await app.run_console()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass