import os
import asyncio
from collections import defaultdict
from dotenv import load_dotenv
import discord
from discord.ext import commands
import anthropic

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Você é um assistente útil integrado ao Discord. Responda de forma clara e concisa.",
)

_raw_channels = os.getenv("ALLOWED_CHANNEL_IDS", "")
ALLOWED_CHANNEL_IDS = (
    {int(c.strip()) for c in _raw_channels.split(",") if c.strip()}
    if _raw_channels.strip()
    else set()
)

# Histórico de conversa por canal (lista de dicts role/content)
conversation_history: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY = 20  # número de mensagens mantidas por canal

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def channel_allowed(channel_id: int) -> bool:
    return not ALLOWED_CHANNEL_IDS or channel_id in ALLOWED_CHANNEL_IDS


async def ask_claude(channel_id: int, user_message: str) -> str:
    history = conversation_history[channel_id]
    history.append({"role": "user", "content": user_message})

    # Mantém apenas as últimas MAX_HISTORY mensagens
    if len(history) > MAX_HISTORY:
        conversation_history[channel_id] = history[-MAX_HISTORY:]
        history = conversation_history[channel_id]

    loop = asyncio.get_event_loop()

    def _call():
        with anthropic_client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history,
            thinking={"type": "adaptive"},
        ) as stream:
            return stream.get_final_message()

    response = await loop.run_in_executor(None, _call)

    reply = next(
        (block.text for block in response.content if block.type == "text"), ""
    )

    history.append({"role": "assistant", "content": reply})
    return reply


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    if ALLOWED_CHANNEL_IDS:
        print(f"Monitorando canais: {ALLOWED_CHANNEL_IDS}")
    else:
        print("Respondendo em qualquer canal onde for mencionado")


@bot.event
async def on_message(message: discord.Message):
    # Ignora mensagens do próprio bot
    if message.author == bot.user:
        return

    # Processa comandos primeiro (!limpar, etc.)
    await bot.process_commands(message)

    # Responde apenas se mencionado ou se o canal estiver na lista permitida
    mentioned = bot.user in message.mentions
    in_allowed = channel_allowed(message.channel.id)

    if not mentioned and not in_allowed:
        return

    # Remove a menção do texto antes de enviar ao Claude
    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(
            f"<@!{mention.id}>", ""
        )
    content = content.strip()

    if not content:
        return

    async with message.channel.typing():
        try:
            reply = await ask_claude(message.channel.id, content)
        except anthropic.APIError as e:
            reply = f"Erro ao contatar o Claude: {e}"

    # Discord limita mensagens a 2000 caracteres
    if len(reply) <= 2000:
        await message.reply(reply)
    else:
        chunks = [reply[i : i + 2000] for i in range(0, len(reply), 2000)]
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


@bot.command(name="limpar")
async def clear_history(ctx: commands.Context):
    """Apaga o histórico de conversa do canal atual."""
    conversation_history[ctx.channel.id].clear()
    await ctx.send("Histórico de conversa apagado.")


@bot.command(name="historico")
async def show_history(ctx: commands.Context):
    """Mostra quantas mensagens estão no histórico do canal."""
    count = len(conversation_history[ctx.channel.id])
    await ctx.send(f"Há {count} mensagem(ns) no histórico deste canal.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
