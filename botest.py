import discord
from discord.ext import commands
from token__ import TOKEN, guild_id, channel
from discord import app_commands
import datetime
from datetime import datetime
from dcclass import memb, membermanager

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

manager=membermanager()

@bot.event
async def on_ready():
    log_message = f'Login bot: {bot.user}'
   # await bot.get_channel(channel).send("봇 ON")
    print(log_message)

#@bot.event
#async def on_disconnect():
 #   await bot.get_channel(channel).send("봇 OFF")

@bot.event
async def on_voice_state_update(member, before, after):
    AB = bot.get_channel(channel)
    if after.channel is not None and before.channel is None:              #입장
        log_message = f'{member.display_name}({member.name}) 입갤'
        if AB:
            manager.making(member.display_name,member.name,'in')
            await AB.send(log_message)
        else:
            print(f"Error: Could not find channel with ID {channel}")

    if before.channel is not None and after.channel is None:                  #퇴장
        log_message = f'{member.display_name}({member.name}) 점점 멀어지네...'
        AB = bot.get_channel(channel)

        if AB:
            manager.making(member.display_name,member.name,'out')
            await AB.send(log_message)
        else:
            print(f"Error: Could not find channel with ID {channel}")

    if before.self_mute != after.self_mute or before.mute != after.mute:
        if after.mute or after.self_mute:
            await AB.send(f"{member.display_name}({member.name}) 음소거")
        else:
            await AB.send(f"{member.display_name}({member.name}) 음소거 해제")

@bot.command()
async def reset(ctx):
    manager.update()
    await ctx.send(manager.printing())
    manager.reset()
    await ctx.send("초기화")

@bot.command()
async def prt(ctx):
    manager.update()
    await ctx.send(manager.printing())


bot.run(TOKEN)

