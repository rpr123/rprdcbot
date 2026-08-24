import gzip
import io
import json
import os
import sqlite3
import zlib
from datetime import datetime, time, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from guild_settings import GuildSettingsStore
from voice_tracker import MemberManager


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or os.getenv("GUILD_ID") or os.getenv("guild_id")

KST = timezone(timedelta(hours=9))
MIDNIGHT = time(hour=0, minute=0, second=0, tzinfo=KST)
TEST_MODE = False
MAX_STATS_FILE_SIZE = 5 * 1024 * 1024
MAX_STATS_UNCOMPRESSED_SIZE = 50 * 1024 * 1024
VOICE_STATS_DB = os.getenv("VOICE_STATS_DB", "voice_time.db")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
settings_store = GuildSettingsStore()
managers = {}


def get_manager(guild_id):
    if guild_id not in managers:
        managers[guild_id] = MemberManager(
            file_name=f"stats_{guild_id}.json",
            guild_id=str(guild_id),
            database_file=VOICE_STATS_DB,
        )
    return managers[guild_id]


def get_channel(channel_id):
    if not channel_id:
        return None
    return bot.get_channel(int(channel_id))


def get_log_channel(guild_id):
    return get_channel(settings_store.get_log_channel_id(guild_id))


def get_settlement_channel(guild_id):
    return get_channel(settings_store.get_settlement_channel_id(guild_id))


async def send_log(guild_id, message):
    channel = get_log_channel(guild_id)
    if channel:
        await channel.send(message)
    else:
        print(f"Log channel is not set for guild {guild_id}")


async def send_in_chunks(channel, message, limit=2000):
    """Send a potentially long settlement without exceeding Discord's limit."""

    remaining = message
    while remaining:
        if len(remaining) <= limit:
            await channel.send(remaining)
            return
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        await channel.send(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")


async def try_send_settlement(guild_id, channel, period_name, message):
    try:
        await send_in_chunks(channel, message)
    except discord.HTTPException as error:
        # 일일 원장은 이미 저장되었으므로 발송 오류가 추적 task를 중단하거나
        # 다른 기간/길드의 정산을 막지 않게 한다.
        print(f"{guild_id} {period_name} 정산 전송 실패: {error}")


async def sync_commands():
    if DEV_GUILD_ID:
        guild = discord.Object(id=int(DEV_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)

    await bot.tree.sync()


def collect_active_voice_members(guild, announce=False):
    active_members = []
    voice_channels = [*guild.voice_channels, *guild.stage_channels]
    for voice_channel in voice_channels:
        for member in voice_channel.members:
            if member.bot:
                continue
            active_members.append((member.display_name, str(member.id)))
            if announce:
                print(
                    f"봇 시작 시 감지: {guild.name} / "
                    f"{member.display_name}({member.name}) "
                    f"- {voice_channel.name}에 접속 중"
                )
    return active_members


@bot.event
async def on_ready():
    ready_at = datetime.now(KST)
    # 첫 await 전에 모든 길드를 reconcile해 stale DB 세션과 음성 이벤트가
    # 섞일 수 있는 startup race를 막는다.
    for guild in bot.guilds:
        manager = get_manager(guild.id)
        manager.map_legacy_members(
            (
                (member.name, str(member.id), member.display_name)
                for member in guild.members
                if not member.bot
            )
        )
        manager.reconcile_active_members(
            collect_active_voice_members(guild, announce=True),
            now=ready_at,
        )

    await sync_commands()

    for guild in bot.guilds:
        manager = get_manager(guild.id)
        await send_log(guild.id, "봇이 켜졌어요.")
        if TEST_MODE:
            await send_log(guild.id, manager.print_week())

    if not midnight_check.is_running():
        midnight_check.start()
    if not auto_save.is_running():
        auto_save.start()

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/print")
    )
    print(f"Login bot: {bot.user}")


@bot.event
async def on_disconnect():
    disconnected_at = datetime.now(KST)
    for manager in managers.values():
        manager.suspend(disconnected_at)
    print("Discord 연결이 끊겨 음성 시간 추적을 일시 중지합니다.")


@bot.event
async def on_resumed():
    resumed_at = datetime.now(KST)
    for guild in bot.guilds:
        get_manager(guild.id).reconcile_active_members(
            collect_active_voice_members(guild),
            now=resumed_at,
        )
    print("Discord 연결이 복구되어 현재 음성 접속자 기준으로 추적을 재개합니다.")


@tasks.loop(time=MIDNIGHT)
async def midnight_check():
    now = datetime.now(KST)

    for guild in bot.guilds:
        manager = get_manager(guild.id)
        # 메시지를 보낼 채널이 없어도 자정까지의 시간은 먼저 일일 원장에 저장한다.
        manager.update(now)
        settlement_channel = get_settlement_channel(guild.id)
        if not settlement_channel:
            print(f"Settlement channel is not set for guild {guild.id}")
            continue

        if now.weekday() == 0:
            await try_send_settlement(
                guild.id,
                settlement_channel,
                "주간",
                manager.print_week(now=now),
            )
        if now.day == 1:
            await try_send_settlement(
                guild.id,
                settlement_channel,
                "월간",
                manager.print_month(now=now),
            )
            if now.month == 1:
                await try_send_settlement(
                    guild.id,
                    settlement_channel,
                    "연간",
                    manager.print_year(now=now),
                )


@tasks.loop(minutes=5)
async def auto_save():
    for manager in managers.values():
        manager.update()
    print("데이터 자동 백업 완료")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    guild_id = member.guild.id
    manager = get_manager(guild_id)

    if after.channel is not None and before.channel is None:
        manager.enter_exit(member.display_name, str(member.id), "in")
        await send_log(guild_id, f"{member.display_name}({member.name}) 입갤")

    elif before.channel is not None and after.channel is None:
        manager.enter_exit(member.display_name, str(member.id), "out")
        await send_log(guild_id, f"{member.display_name}({member.name}) 점점 멀어지네...")

    elif (
        before.channel is not None
        and after.channel is not None
        and before.channel != after.channel
    ):
        manager.enter_exit(member.display_name, str(member.id), "out")
        manager.enter_exit(member.display_name, str(member.id), "in")
        await send_log(
            guild_id,
            f"{member.display_name}({member.name}) 채널 이동: "
            f"{before.channel.name} → {after.channel.name}",
        )

    if before.self_mute != after.self_mute or before.mute != after.mute:
        status = "음소거" if after.mute or after.self_mute else "음소거 해제"
        await send_log(guild_id, f"{member.display_name}({member.name}) {status}")


@bot.tree.command(name="set_log_channel", description="입퇴장 로그 채널을 설정합니다")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def set_log_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    settings_store.set_log_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        f"로그 채널을 {channel.mention}(으)로 설정했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="set_settlement_channel", description="주간/월간 정산 채널을 설정합니다")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def set_settlement_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    settings_store.set_settlement_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        f"정산 채널을 {channel.mention}(으)로 설정했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="settings", description="현재 서버의 봇 채널 설정을 확인합니다")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def settings(interaction: discord.Interaction):
    guild_settings = settings_store.get(interaction.guild_id)
    log_channel_id = guild_settings.get("log_channel_id")
    settlement_channel_id = guild_settings.get("settlement_channel_id")

    log_channel = f"<#{log_channel_id}>" if log_channel_id else "미설정"
    settlement_channel = (
        f"<#{settlement_channel_id}>" if settlement_channel_id else "미설정"
    )

    await interaction.response.send_message(
        f"로그 채널: {log_channel}\n정산 채널: {settlement_channel}",
        ephemeral=True,
    )


@bot.tree.command(name="reset", description="현상태를 출력 후 초기화합니다")
@app_commands.guild_only()
async def reset(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    manager.update()
    await interaction.response.send_message(manager.print_current())
    manager.reset()
    await interaction.followup.send("초기화")


@bot.tree.command(name="print", description="현상태를 출력합니다")
@app_commands.guild_only()
async def prt(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    manager.update()
    await interaction.response.send_message(manager.print_current())


@bot.tree.command(name="get_json", description="현재 서버의 일일 통계를 JSON으로 내보냅니다")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def get_json(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    manager.update()
    payload = json.dumps(
        manager.export_stats(),
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    filename = f"stats_{interaction.guild_id}_daily.json"
    if len(payload) > MAX_STATS_UNCOMPRESSED_SIZE:
        await interaction.response.send_message(
            "통계 백업 JSON이 50MB를 초과합니다. "
            "운영 환경의 SQLite 온라인 백업을 사용해 주세요.",
            ephemeral=True,
        )
        return
    if len(payload) > MAX_STATS_FILE_SIZE:
        payload = gzip.compress(payload)
        filename += ".gz"
    if len(payload) > MAX_STATS_FILE_SIZE:
        await interaction.response.send_message(
            "통계 백업이 압축 후에도 5MB를 초과합니다. "
            "운영 환경의 SQLite 온라인 백업을 사용해 주세요.",
            ephemeral=True,
        )
        return
    export_file = discord.File(
        io.BytesIO(payload),
        filename=filename,
    )
    await interaction.response.send_message(
        "현재 서버의 일일 통계 백업입니다.",
        file=export_file,
        ephemeral=True,
    )


@bot.tree.command(name="upload_json", description="일일 통계 JSON을 현재 서버에 복원합니다")
@app_commands.describe(file="복원할 일일 JSON/JSON.GZ 또는 보관할 기존 stats.json")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def upload_json(
    interaction: discord.Interaction,
    file: discord.Attachment,
):
    lower_filename = file.filename.lower()
    if not lower_filename.endswith((".json", ".json.gz")):
        await interaction.response.send_message(
            "JSON 또는 JSON.GZ 파일만 업로드할 수 있습니다.",
            ephemeral=True,
        )
        return

    if file.size > MAX_STATS_FILE_SIZE:
        await interaction.response.send_message(
            "통계 파일은 5MB 이하여야 합니다.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        contents = await file.read()
        if lower_filename.endswith(".json.gz"):
            with gzip.GzipFile(fileobj=io.BytesIO(contents)) as compressed_file:
                contents = compressed_file.read(MAX_STATS_UNCOMPRESSED_SIZE + 1)
        if len(contents) > MAX_STATS_UNCOMPRESSED_SIZE:
            await interaction.followup.send(
                "압축을 푼 통계 JSON은 50MB 이하여야 합니다.",
                ephemeral=True,
            )
            return
        stats = json.loads(contents.decode("utf-8-sig"))
        import_type = get_manager(interaction.guild_id).replace_stats(stats)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        gzip.BadGzipFile,
        EOFError,
        zlib.error,
    ):
        await interaction.followup.send(
            "UTF-8 형식의 올바른 JSON 파일이 아닙니다.",
            ephemeral=True,
        )
        return
    except ValueError as error:
        await interaction.followup.send(
            f"통계 파일 형식이 올바르지 않습니다: {error}",
            ephemeral=True,
        )
        return
    except (discord.HTTPException, OSError, sqlite3.DatabaseError):
        await interaction.followup.send(
            "통계 파일을 읽거나 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    if import_type == "daily":
        message = "업로드한 일일 통계로 현재 서버의 기존 통계를 교체했습니다."
    else:
        message = (
            "기존 형식 통계는 날짜별로 정확히 변환할 수 없어 원문을 보관했습니다. "
            "새 정산에는 전환 이후 저장된 일일 기록만 사용됩니다."
        )
    await interaction.followup.send(message, ephemeral=True)


@set_log_channel.error
@set_settlement_channel.error
@settings.error
@get_json.error
@upload_json.error
async def admin_command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "서버 관리 권한이 있는 사람만 사용할 수 있습니다.",
            ephemeral=True,
        )


if __name__ == "__main__":
    bot.run(TOKEN)
