import json
import os
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

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
settings_store = GuildSettingsStore()
managers = {}


def get_manager(guild_id):
    if guild_id not in managers:
        managers[guild_id] = MemberManager(file_name=f"stats_{guild_id}.json")
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


async def sync_commands():
    if DEV_GUILD_ID:
        guild = discord.Object(id=int(DEV_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)

    await bot.tree.sync()


@bot.event
async def on_ready():
    await sync_commands()

    for guild in bot.guilds:
        manager = get_manager(guild.id)
        manager.load_in_progress_data()

        for voice_channel in guild.voice_channels:
            for member in voice_channel.members:
                # 봇 자신은 제외
                if not member.bot:
                    manager.enter_exit(member.display_name, member.name, "in")
                    print(
                        f"봇 시작 시 감지: {guild.name} / "
                        f"{member.display_name}({member.name}) "
                        f"- {voice_channel.name}에 접속 중"
                    )

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


@tasks.loop(time=MIDNIGHT)
async def midnight_check():
    now = datetime.now()

    for guild in bot.guilds:
        settlement_channel = get_settlement_channel(guild.id)
        if not settlement_channel:
            print(f"Settlement channel is not set for guild {guild.id}")
            continue

        manager = get_manager(guild.id)
        if now.weekday() == 0:
            await settlement_channel.send(manager.print_week())
        if now.day == 1:
            await settlement_channel.send(manager.print_month())
            if now.month == 1:
                await settlement_channel.send(manager.print_year())


@tasks.loop(minutes=5)
async def auto_save():
    for manager in managers.values():
        manager.update()
        manager.save_in_progress_data()
        manager.save_stats()
    print("데이터 자동 백업 완료")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    guild_id = member.guild.id
    manager = get_manager(guild_id)

    if after.channel is not None and before.channel is None:
        manager.enter_exit(member.display_name, member.name, "in")
        await send_log(guild_id, f"{member.display_name}({member.name}) 입갤")

    elif before.channel is not None and after.channel is None:
        manager.enter_exit(member.display_name, member.name, "out")
        await send_log(guild_id, f"{member.display_name}({member.name}) 점점 멀어지네...")

    elif (
        before.channel is not None
        and after.channel is not None
        and before.channel != after.channel
    ):
        manager.enter_exit(member.display_name, member.name, "out")
        manager.enter_exit(member.display_name, member.name, "in")
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


@bot.tree.command(name="get_json", description="현재 서버의 통계 파일을 업로드합니다")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def get_json(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    manager.update()
    manager.save_stats()

    if os.path.exists(manager.file_name):
        await interaction.response.send_message(
            "현재 서버의 통계 파일입니다.",
            file=discord.File(manager.file_name),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "파일이 아직 생성되지 않았습니다.",
            ephemeral=True,
        )


@bot.tree.command(name="upload_json", description="통계 JSON 파일을 현재 서버에 복원합니다")
@app_commands.describe(file="복원할 stats.json 파일")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def upload_json(
    interaction: discord.Interaction,
    file: discord.Attachment,
):
    if not file.filename.lower().endswith(".json"):
        await interaction.response.send_message(
            "JSON 파일만 업로드할 수 있습니다.",
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
        stats = json.loads(contents.decode("utf-8-sig"))
        get_manager(interaction.guild_id).replace_stats(stats)
    except (UnicodeDecodeError, json.JSONDecodeError):
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
    except (discord.HTTPException, OSError):
        await interaction.followup.send(
            "통계 파일을 읽거나 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        "업로드한 통계로 현재 서버의 기존 통계를 교체했습니다.",
        ephemeral=True,
    )


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


bot.run(TOKEN)
