import os
import typing

import discord
from discord.ext import commands
import asyncio
import datetime
import io
import json
import time
import typing
from typing import Literal

import aiohttp
from discord import app_commands
from elevate_ai.AsyncClient import AsyncClient

BOT_PREFIX = ";;"
USE_ATTACHMENT_LINKS = False
BOT_TOKEN = "<DISCORD BOT TOKEN>"
ELEVATE_TOKEN = '<ELEVATEAI TOKEN>'

AUDIO_UPLOAD_TIMEOUT_SECS = 360
STATUS_CHECK_FREQUENCY_SECS = 30

STATUS_EXPLANATIONS = {
    'declared': 'Your audio interaction is declared and waiting in the queue.',
    'filePendingUpload': 'Your audio interaction is declared, but the file is pending upload.',
    'fileUploading': 'Your file is currently being uploaded to the API.',
    'fileUploaded': 'Your file has been successfully uploaded and is waiting for processing.',
    'fileUploadFailed': 'An error occurred during the upload process. Please try again.',
    'filePendingDownload': 'Your file is in the queue to be downloaded.',
    'fileDownloading': 'Your file is currently being downloaded from the provided URL.',
    'fileDownloaded': 'Your file has been downloaded successfully and is waiting for processing.',
    'fileDownloadFailed': 'An error occurred while downloading the file. Please check the format and try again.',
    'pendingProcessing': 'Your interaction is in the queue for processing.',
    'processing': 'Your interaction is being actively processed.',
    'processed': 'Your interaction has been successfully processed. You can now retrieve the transcript.',
    'processingFailed': 'An error occurred during processing. Please contact support.'
}

elevate_client = AsyncClient('https://api.elevateai.com/v1', ELEVATE_TOKEN)
active_interactions = {}
bot = commands.Bot(
    description="A bot for transcribing audio with ElevateAI.",
    command_prefix=BOT_PREFIX,
    intents=discord.Intents.all(),
    help_command=commands.DefaultHelpCommand(no_category='Other')
)

class AudioInteraction:
    def __init__(self, identifier: str, user_id: int, channel_id: int, guild_id: int, cached_status: str,
                 last_status_update: int):
        self.identifier = identifier
        self.user_id = user_id
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.cached_status = cached_status
        self.last_status_update = last_status_update


def convert_to_readable_transcript(api_response):
    readable_transcript_lines = []

    for segment in api_response['sentenceSegments']:
        participant = segment['participant']
        start_time = segment['startTimeOffset']
        end_time = segment['endTimeOffset']
        confidence_score = segment['score']
        phrase = segment['phrase']

        readable_transcript_lines.append(f"{participant}:({start_time}-{end_time}):{confidence_score}: {phrase}")

    return "\n".join(readable_transcript_lines)


class AttachFileView(discord.ui.View):
    def __init__(self, bot: commands.Bot, ctx: commands.Context, timeout: float, auto_defer: bool = True):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.bot = bot
        self.cancelled = False
        self.bot.add_listener(self.message_listener, 'on_message')
        self.result: discord.Attachment | None = None
        self.new_interaction = None
        self.auto_defer = auto_defer

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.auto_defer:
            await interaction.response.defer()
        self.cancelled = True
        self.new_interaction = interaction
        self.stop()
        self.cleanup()

    def cleanup(self):
        self.bot.remove_listener(self.message_listener, 'on_message')

    async def on_timeout(self):
        if self.cancelled:
            return

        self.cleanup()

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user == self.ctx.author and interaction.channel == self.ctx.channel

    async def message_listener(self, message: discord.Message):
        if message.author == self.ctx.author and message.channel == self.ctx.channel and message.attachments:
            self.result = message.attachments[0]
            self.stop()
            self.cleanup()
            return


@commands.guild_only()
@bot.hybrid_command(name="transcribe", brief="Transcribes an audio file. Please attach a file to your message.")
@app_commands.describe(
    language="The language to transcribe in. Defaults to en-us. Options: en-us, en, es-419 (NA spanish), pt-br (Brazilian Portuguese)"
)
async def transcribe(ctx: commands.Context, *,
                     language: typing.Literal['en-us', 'en', 'es-419', 'pt-br'] = 'en-us'):
    language = language.lower()
    if language not in ['en-us', 'en', 'es-419', 'pt-br']:
        await ctx.send("Sorry, that's not a valid language. Please choose one of the following: en-us, en, es-419, pt-br", ephemeral=True)
        return
    if not ctx.message.attachments:
        view = AttachFileView(bot, ctx, timeout=AUDIO_UPLOAD_TIMEOUT_SECS)
        await ctx.send(
            "Please send the audio file in the next 5 minutes, or press the cancel button to cancel. Note, you can invoke the command with the file already attached.",
            view=view,
            ephemeral=True
        )
        is_timeout = await view.wait()

        if is_timeout:
            await ctx.send("Sorry, you took too long to attach an audio file. Please try again.", ephemeral=True)
            return

        if view.cancelled:
            await ctx.send("You've cancelled the command.", ephemeral=True)
            return

        attachment = view.result
    else:
        attachment = ctx.message.attachments[0]

    kwargs = {}
    if USE_ATTACHMENT_LINKS:
        file_url = attachment.url
        kwargs['url'] = file_url
    else:
        kwargs['mediafile'] = await attachment.read()
        kwargs['bytesUploadName'] = attachment.filename

    try:
        interaction = await elevate_client.declare(languageTag=language, **kwargs)
    except aiohttp.ClientError as e:
        await ctx.send("An error occurred while uploading your file.", ephemeral=True)
        return

    identifier = interaction['interactionIdentifier']

    await ctx.send(f"Transcription has started. To check its status, you can use the command `/check {identifier}`. You'll receive a DM when it's done.", ephemeral=True)
    active_interactions[identifier] = AudioInteraction(identifier, ctx.author.id, ctx.channel.id, ctx.guild.id,
                                                       interaction['status'], int(time.time()))

    while True:
        await asyncio.sleep(STATUS_CHECK_FREQUENCY_SECS)

        status = await elevate_client.status(identifier)
        active_interactions[identifier].cached_status = status
        active_interactions[identifier].last_status_update = int(time.time())

        if status == 'processed':
            del active_interactions[identifier]
            break

        if status in ('processingFailed', 'fileDownloadFailed', 'fileUploadFailed'):
            status_explanation = STATUS_EXPLANATIONS.get(status, f"Unknown status: {status}")
            await ctx.author.send(f"Your transcript with identifier `{identifier}` failed to generate. Status: {status_explanation}")
            del active_interactions[identifier]
            return

    transcript_data = await elevate_client.transcripts(identifier)
    if transcript_data is False:
        await ctx.author.send(f"Your transcript with the identifier `{identifier}` came up empty.")
        return

    transcript_text = convert_to_readable_transcript(transcript_data)

    transcript_file = discord.File(fp=io.BytesIO(transcript_text.encode()), filename="transcript.txt")
    transcript_json = discord.File(fp=io.BytesIO(json.dumps(transcript_data, indent=4).encode()),
                                   filename="transcript.json")
    await ctx.author.send(f"Your transcript with identifier `{identifier}` is ready! Please see the attached files.", files=[transcript_file, transcript_json])


@commands.guild_only()
@bot.hybrid_command(name="check", brief="Get the status of your transcription.")
async def status(ctx: commands.Context, identifier: str):
    if identifier not in active_interactions:
        await ctx.send(f"Sorry, I couldn't find an interaction with the identifier `{identifier}`.", ephemeral=True)
        return

    interaction: AudioInteraction = active_interactions[identifier]
    if interaction.user_id != ctx.author.id and not ctx.author.guild_permissions.administrator:
        await ctx.send(f"Sorry, you're not the author of the interaction with the identifier `{identifier}`.", ephemeral=True)
        return

    if interaction.cached_status is None:
        await ctx.send(f"Sorry, the interaction with the identifier `{identifier}` hasn't started yet.", ephemeral=True)
        return

    status_explanation = STATUS_EXPLANATIONS.get(interaction.cached_status, f"Unknown status: {interaction.cached_status}")

    await ctx.send(f"""Interaction with identifier `{identifier}`:
Status: {status_explanation}
Last updated: <t:{interaction.last_status_update}:F> (<t:{interaction.last_status_update}:R>)

Please wait for a bit before using this command again.""", ephemeral=True)

# helper command to sync commands with discord. use ";;sync *" for testing
@bot.command()
@commands.guild_only()
@commands.is_owner()
async def sync(
        ctx: commands.Context, guilds: commands.Greedy[discord.Object],
        spec: typing.Literal["~", "*", "^", "^^", "??", "?"] | None = None) -> None:
    """
    Works like:
    !sync -> global sync
    !sync ~ -> sync current guild
    !sync * -> copies all global app commands to current guild and syncs
    !sync ^ -> clears all commands from the current guild target and syncs (removes guild commands)
    !sync ^^ -> clears all global commands and syncs
    !sync ? -> shows current guild's commands
    !sync ?? -> shows global commands
    !sync id_1 id_2 -> syncs guilds with id 1 and 2

    :param ctx:
    :param guilds:
    :param spec:
    :return:
    """

    if not guilds:
        if spec == "~":
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
        elif spec == "*":
            ctx.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
        elif spec == "^":
            commands_backup = ctx.bot.tree.get_commands(guild=ctx.guild)
            ctx.bot.tree.clear_commands(guild=ctx.guild)
            await ctx.bot.tree.sync(guild=ctx.guild)
            for command in commands_backup:
                ctx.bot.tree.add_command(command, guild=ctx.guild)
            synced = []
        elif spec == "^^":
            commands_backup = ctx.bot.tree.get_commands(guild=None)
            ctx.bot.tree.clear_commands(guild=None)
            await ctx.bot.tree.sync()
            for command in commands_backup:
                ctx.bot.tree.add_command(command, guild=None)
            synced = []
        elif spec == '?' or spec == '??':
            if spec == '?':
                cms = ctx.bot.tree.get_commands(guild=ctx.guild)
            else:
                cms = ctx.bot.tree.get_commands()

            synced_str_parts = []
            for app_command in cms:
                synced_str_parts.append(
                    f"{app_command.name} ({app_command.type}): {app_command.description if hasattr(app_command, 'description') else 'No description'}")
            synced_str = "\n\n".join(synced_str_parts)

            await ctx.send(
                f"{len(cms)} commands {'globally' if spec == '??' else 'in this guild'}:\n"
                f"```\n{synced_str}\n```"
            )
            return
        elif spec is not None:
            raise commands.BadArgument("Invalid spec")
        else:
            synced = await ctx.bot.tree.sync()

        synced_str_parts = []
        for app_command in synced:
            synced_str_parts.append(f"{app_command.name} ({app_command.type}): {app_command.description}")
        synced_str = "\n\n".join(synced_str_parts)
        await ctx.send(
            f"Synced {len(synced)} commands {'globally' if spec is None else 'to the current guild.'}\n\n```\n{synced_str}\n```")
        return

    ret = 0
    for guild in guilds:
        try:
            await ctx.bot.tree.sync(guild=guild)
        except discord.HTTPException:
            pass
        else:
            ret += 1

    await ctx.send(f"Synced the tree to {ret}/{len(guilds)}.")


bot.run(os.getenv('BOT_TOKEN', BOT_TOKEN))
