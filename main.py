import os
import asyncio
from collections import defaultdict
from dotenv import load_dotenv
import discord
from discord.ext import commands
import anthropic

SUMMARY_LIMIT_DEFAULT = 100  # mensagens buscadas por padrão no !resumir

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


async def _send_long(channel: discord.abc.Messageable, text: str, reference=None):
    """Envia texto dividido em chunks de 2000 chars."""
    chunks = [text[i : i + 2000] for i in range(0, len(text), 2000)]
    for i, chunk in enumerate(chunks):
        if i == 0 and reference:
            await reference.reply(chunk)
        else:
            await channel.send(chunk)


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

    # Processa comandos primeiro (!resumir, !limpar, etc.)
    await bot.process_commands(message)

    # Ignora mensagens que são comandos do bot
    if message.content.startswith(COMMAND_PREFIX):
        return

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

    await _send_long(message.channel, reply, reference=message)


async def _fetch_messages(
    channel: discord.TextChannel, limit: int
) -> list[discord.Message]:
    """Busca as últimas `limit` mensagens do canal em ordem cronológica."""
    msgs = []
    async for msg in channel.history(limit=limit, oldest_first=False):
        msgs.append(msg)
    msgs.reverse()
    return msgs


def _format_messages_for_claude(messages: list[discord.Message]) -> str:
    """Formata mensagens do Discord em texto para o Claude processar."""
    lines = []
    for msg in messages:
        author = msg.author.display_name
        timestamp = msg.created_at.strftime("%d/%m/%Y %H:%M")
        content = msg.content or "[sem texto]"
        if msg.attachments:
            content += f" [anexos: {', '.join(a.filename for a in msg.attachments)}]"
        lines.append(f"[{timestamp}] {author}: {content}")
    return "\n".join(lines)


async def _summarize_with_claude(raw_text: str, extra_instruction: str = "") -> str:
    """Pede ao Claude para resumir o texto das mensagens."""
    instruction = (
        "Você é um assistente que resume conversas do Discord. "
        "Abaixo estão mensagens de um canal no formato [data/hora] autor: mensagem. "
        "Faça um resumo claro e organizado dos principais assuntos discutidos."
    )
    if extra_instruction:
        instruction += f" {extra_instruction}"

    prompt = f"{instruction}\n\nMensagens:\n{raw_text}"

    loop = asyncio.get_event_loop()

    def _call():
        with anthropic_client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_message()

    response = await loop.run_in_executor(None, _call)
    return next(
        (block.text for block in response.content if block.type == "text"), ""
    )


@bot.command(name="resumir")
async def summarize_channel(ctx: commands.Context, quantidade: int = SUMMARY_LIMIT_DEFAULT):
    """Resume as últimas mensagens do canal atual.

    Uso: !resumir [quantidade]
    Exemplo: !resumir 50  → resume as últimas 50 mensagens
    """
    if quantidade < 1 or quantidade > 500:
        await ctx.send("Informe um número entre 1 e 500.")
        return

    status = await ctx.send(f"Lendo as últimas {quantidade} mensagens… ⏳")

    messages = await _fetch_messages(ctx.channel, quantidade)

    if not messages:
        await status.edit(content="Nenhuma mensagem encontrada no canal.")
        return

    raw_text = _format_messages_for_claude(messages)

    await status.edit(content=f"Resumindo {len(messages)} mensagens com o Claude… 🤖")

    try:
        summary = await _summarize_with_claude(raw_text)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    header = f"**Resumo das últimas {len(messages)} mensagens de #{ctx.channel.name}:**\n\n"
    await _send_long(ctx.channel, header + summary)


@bot.command(name="resumir_canal")
async def summarize_other_channel(ctx: commands.Context, canal: discord.TextChannel, quantidade: int = SUMMARY_LIMIT_DEFAULT):
    """Resume mensagens de outro canal.

    Uso: !resumir_canal #canal [quantidade]
    Exemplo: !resumir_canal #geral 100
    """
    if quantidade < 1 or quantidade > 500:
        await ctx.send("Informe um número entre 1 e 500.")
        return

    if not canal.permissions_for(ctx.guild.me).read_message_history:
        await ctx.send(f"Não tenho permissão para ler o histórico de {canal.mention}.")
        return

    status = await ctx.send(f"Lendo as últimas {quantidade} mensagens de {canal.mention}… ⏳")

    messages = await _fetch_messages(canal, quantidade)

    if not messages:
        await status.edit(content="Nenhuma mensagem encontrada nesse canal.")
        return

    raw_text = _format_messages_for_claude(messages)

    await status.edit(content=f"Resumindo {len(messages)} mensagens com o Claude… 🤖")

    try:
        summary = await _summarize_with_claude(raw_text)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    header = f"**Resumo das últimas {len(messages)} mensagens de {canal.mention}:**\n\n"
    await _send_long(ctx.channel, header + summary)


@bot.command(name="historico")
async def show_history(ctx: commands.Context):
    """Mostra quantas mensagens estão no histórico do canal."""
    count = len(conversation_history[ctx.channel.id])
    await ctx.send(f"Há {count} mensagem(ns) no histórico deste canal.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
