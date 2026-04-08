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
MY_DISCORD_ID = int(os.getenv("MY_DISCORD_ID", "0"))
MY_DISCORD_NAME = os.getenv("MY_DISCORD_NAME", "")

_raw_allowed = os.getenv("ALLOWED_CHANNEL_IDS", "")
ALLOWED_CHANNEL_IDS = (
    {int(c.strip()) for c in _raw_allowed.split(",") if c.strip()}
    if _raw_allowed.strip()
    else set()
)

_raw_monitored = os.getenv("MONITORED_CHANNELS", "")
MONITORED_CHANNELS = (
    [int(c.strip()) for c in _raw_monitored.split(",") if c.strip()]
    if _raw_monitored.strip()
    else []
)

DEFAULT_LIMIT = 100   # mensagens por canal nos comandos padrão
MAX_LIMIT = 500

conversation_history: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY = 20

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _send_long(channel: discord.abc.Messageable, text: str, reference=None):
    """Envia texto dividido em chunks de 2000 chars."""
    chunks = [text[i : i + 2000] for i in range(0, len(text), 2000)]
    for i, chunk in enumerate(chunks):
        if i == 0 and reference:
            await reference.reply(chunk)
        else:
            await channel.send(chunk)


async def _fetch_messages(channel: discord.TextChannel, limit: int) -> list[discord.Message]:
    """Busca as últimas `limit` mensagens em ordem cronológica."""
    msgs = []
    async for msg in channel.history(limit=limit, oldest_first=False):
        msgs.append(msg)
    msgs.reverse()
    return msgs


def _format_messages(messages: list[discord.Message], channel_name: str = "") -> str:
    """Formata mensagens para o Claude."""
    lines = []
    if channel_name:
        lines.append(f"=== #{channel_name} ===")
    for msg in messages:
        author = msg.author.display_name
        ts = msg.created_at.strftime("%d/%m/%Y %H:%M")
        content = msg.content or "[sem texto]"
        if msg.attachments:
            content += f" [anexos: {', '.join(a.filename for a in msg.attachments)}]"
        # Indica explicitamente menções para o Claude identificar
        mentions = [f"@{u.display_name}" for u in msg.mentions]
        if mentions:
            content += f" (menciona: {', '.join(mentions)})"
        lines.append(f"[{ts}] {author}: {content}")
    return "\n".join(lines)


async def _call_claude(prompt: str, max_tokens: int = 2048) -> str:
    """Faz uma chamada simples ao Claude sem histórico."""
    loop = asyncio.get_event_loop()

    def _call():
        with anthropic_client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_message()

    response = await loop.run_in_executor(None, _call)
    return next((b.text for b in response.content if b.type == "text"), "")


async def _get_monitored_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    """Retorna os canais monitorados configurados, com permissão de leitura."""
    channels = []
    for cid in MONITORED_CHANNELS:
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).read_message_history:
            channels.append(ch)
    return channels


async def _collect_channel_data(channels: list[discord.TextChannel], limit: int) -> str:
    """Busca mensagens de vários canais e retorna texto consolidado."""
    parts = []
    for ch in channels:
        msgs = await _fetch_messages(ch, limit)
        if msgs:
            parts.append(_format_messages(msgs, channel_name=ch.name))
    return "\n\n".join(parts)


# ── conversa com Claude ───────────────────────────────────────────────────────

async def ask_claude(channel_id: int, user_message: str) -> str:
    history = conversation_history[channel_id]
    history.append({"role": "user", "content": user_message})

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
    reply = next((b.text for b in response.content if b.type == "text"), "")
    history.append({"role": "assistant", "content": reply})
    return reply


# ── eventos ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    if MONITORED_CHANNELS:
        print(f"Canais monitorados para briefing: {MONITORED_CHANNELS}")
    if MY_DISCORD_NAME:
        print(f"Usuário configurado: {MY_DISCORD_NAME} (ID: {MY_DISCORD_ID})")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    if message.content.startswith(COMMAND_PREFIX):
        return

    mentioned = bot.user in message.mentions
    in_allowed = not ALLOWED_CHANNEL_IDS or message.channel.id in ALLOWED_CHANNEL_IDS

    if not mentioned and not in_allowed:
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    if not content:
        return

    async with message.channel.typing():
        try:
            reply = await ask_claude(message.channel.id, content)
        except anthropic.APIError as e:
            reply = f"Erro ao contatar o Claude: {e}"

    await _send_long(message.channel, reply, reference=message)


# ── comandos ──────────────────────────────────────────────────────────────────

@bot.command(name="resumir")
async def cmd_resumir(ctx: commands.Context, quantidade: int = DEFAULT_LIMIT):
    """Resume as últimas mensagens do canal atual.
    Uso: !resumir [quantidade]
    """
    if not 1 <= quantidade <= MAX_LIMIT:
        await ctx.send(f"Informe um número entre 1 e {MAX_LIMIT}.")
        return

    status = await ctx.send(f"Lendo as últimas {quantidade} mensagens… ⏳")
    msgs = await _fetch_messages(ctx.channel, quantidade)

    if not msgs:
        await status.edit(content="Nenhuma mensagem encontrada.")
        return

    await status.edit(content=f"Resumindo {len(msgs)} mensagens com o Claude… 🤖")

    prompt = (
        "Você é um assistente que resume conversas do Discord.\n"
        "Abaixo estão mensagens de um canal no formato [data/hora] autor: mensagem.\n"
        "Faça um resumo claro e organizado dos principais assuntos discutidos, "
        "agrupando por tópico quando possível.\n\n"
        f"Mensagens:\n{_format_messages(msgs, ctx.channel.name)}"
    )

    try:
        summary = await _call_claude(prompt)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    await _send_long(ctx.channel, f"**📋 Resumo de #{ctx.channel.name}**\n\n{summary}")


@bot.command(name="resumir_canal")
async def cmd_resumir_canal(ctx: commands.Context, canal: discord.TextChannel, quantidade: int = DEFAULT_LIMIT):
    """Resume mensagens de outro canal.
    Uso: !resumir_canal #canal [quantidade]
    """
    if not 1 <= quantidade <= MAX_LIMIT:
        await ctx.send(f"Informe um número entre 1 e {MAX_LIMIT}.")
        return

    if not canal.permissions_for(ctx.guild.me).read_message_history:
        await ctx.send(f"Sem permissão para ler {canal.mention}.")
        return

    status = await ctx.send(f"Lendo {canal.mention}… ⏳")
    msgs = await _fetch_messages(canal, quantidade)

    if not msgs:
        await status.edit(content="Nenhuma mensagem encontrada.")
        return

    await status.edit(content=f"Resumindo {len(msgs)} mensagens com o Claude… 🤖")

    prompt = (
        "Você é um assistente que resume conversas do Discord.\n"
        "Faça um resumo claro e organizado dos principais assuntos discutidos, "
        "agrupando por tópico quando possível.\n\n"
        f"Mensagens:\n{_format_messages(msgs, canal.name)}"
    )

    try:
        summary = await _call_claude(prompt)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    await _send_long(ctx.channel, f"**📋 Resumo de {canal.mention}**\n\n{summary}")


@bot.command(name="pendencias")
async def cmd_pendencias(ctx: commands.Context, quantidade: int = DEFAULT_LIMIT):
    """Escaneia os canais monitorados e lista o que está esperando por você.
    Uso: !pendencias [quantidade]
    """
    if not MY_DISCORD_NAME and not MY_DISCORD_ID:
        await ctx.send(
            "Configure `MY_DISCORD_ID` e/ou `MY_DISCORD_NAME` no `.env` "
            "para eu saber quem é você."
        )
        return

    channels = await _get_monitored_channels(ctx.guild)
    if not channels:
        await ctx.send(
            "Nenhum canal monitorado configurado. "
            "Adicione `MONITORED_CHANNELS` no `.env`."
        )
        return

    status = await ctx.send(f"Escaneando {len(channels)} canal(is)… ⏳")
    raw = await _collect_channel_data(channels, quantidade)

    if not raw:
        await status.edit(content="Nenhuma mensagem encontrada nos canais monitorados.")
        return

    await status.edit(content="Analisando pendências com o Claude… 🔍")

    user_ref = MY_DISCORD_NAME or f"ID {MY_DISCORD_ID}"
    prompt = (
        f"Você é um assistente de produtividade pessoal integrado ao Discord.\n"
        f"O usuário se chama **{user_ref}**.\n\n"
        "Analise as mensagens abaixo e identifique SOMENTE os itens que estão "
        f"aguardando uma ação, resposta ou decisão de **{user_ref}**. Inclua:\n"
        "- Perguntas feitas diretamente a ele/ela\n"
        "- Menções esperando resposta\n"
        "- Tarefas ou solicitações direcionadas a ele/ela\n"
        "- Decisões que dependem dele/dela\n\n"
        "Para cada item, informe: canal, autor que aguarda, assunto e data/hora.\n"
        "Se não houver pendências, diga claramente que não há nada pendente.\n\n"
        f"Mensagens dos canais:\n{raw}"
    )

    try:
        result = await _call_claude(prompt, max_tokens=2048)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    await _send_long(ctx.channel, f"**📌 Pendências para {user_ref}**\n\n{result}")


@bot.command(name="parados")
async def cmd_parados(ctx: commands.Context, quantidade: int = DEFAULT_LIMIT):
    """Escaneia os canais monitorados e identifica assuntos parados que precisam de movimentação.
    Uso: !parados [quantidade]
    """
    channels = await _get_monitored_channels(ctx.guild)
    if not channels:
        await ctx.send(
            "Nenhum canal monitorado configurado. "
            "Adicione `MONITORED_CHANNELS` no `.env`."
        )
        return

    status = await ctx.send(f"Escaneando {len(channels)} canal(is)… ⏳")
    raw = await _collect_channel_data(channels, quantidade)

    if not raw:
        await status.edit(content="Nenhuma mensagem encontrada.")
        return

    await status.edit(content="Identificando assuntos parados com o Claude… 🔍")

    prompt = (
        "Você é um assistente de produtividade integrado ao Discord.\n\n"
        "Analise as mensagens abaixo e identifique conversas ou assuntos que:\n"
        "- Ficaram sem resposta ou sem continuidade\n"
        "- Têm perguntas abertas que ninguém respondeu\n"
        "- Estão estagnados e precisam de uma decisão ou ação para avançar\n"
        "- Foram iniciados mas nunca concluídos\n\n"
        "Para cada item parado, informe:\n"
        "  - Canal e data da última mensagem\n"
        "  - O que está travado ou sem resposta\n"
        "  - Quem deveria agir (se identificável)\n"
        "  - Sugestão do próximo passo\n\n"
        "Agrupe por urgência: 🔴 Urgente / 🟡 Atenção / 🟢 Pode esperar.\n"
        "Se não houver nada parado, diga claramente.\n\n"
        f"Mensagens dos canais:\n{raw}"
    )

    try:
        result = await _call_claude(prompt, max_tokens=2048)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    await _send_long(ctx.channel, f"**🚦 Assuntos parados / sem movimentação**\n\n{result}")


@bot.command(name="briefing")
async def cmd_briefing(ctx: commands.Context, quantidade: int = DEFAULT_LIMIT):
    """Relatório completo: resumo + pendências + assuntos parados.
    Uso: !briefing [quantidade]
    """
    channels = await _get_monitored_channels(ctx.guild)
    if not channels:
        await ctx.send(
            "Nenhum canal monitorado configurado. "
            "Adicione `MONITORED_CHANNELS` no `.env`."
        )
        return

    status = await ctx.send(f"Preparando briefing de {len(channels)} canal(is)… ⏳")
    raw = await _collect_channel_data(channels, quantidade)

    if not raw:
        await status.edit(content="Nenhuma mensagem encontrada.")
        return

    user_ref = MY_DISCORD_NAME or (f"ID {MY_DISCORD_ID}" if MY_DISCORD_ID else "o usuário")

    await status.edit(content="Analisando tudo com o Claude… 🤖")

    prompt = (
        f"Você é um assistente executivo integrado ao Discord do usuário **{user_ref}**.\n\n"
        "Com base nas mensagens abaixo, gere um briefing completo em 3 seções:\n\n"
        "## 1. 📋 Resumo dos canais\n"
        "Resuma os principais assuntos discutidos em cada canal, de forma objetiva.\n\n"
        "## 2. 📌 Pendências para você\n"
        f"Liste tudo que está aguardando uma ação, resposta ou decisão de **{user_ref}**: "
        "perguntas, menções, solicitações e tarefas direcionadas a ele/ela.\n\n"
        "## 3. 🚦 Assuntos parados\n"
        "Liste conversas ou tarefas que estão estagnadas e precisam de movimento. "
        "Agrupe por urgência: 🔴 Urgente / 🟡 Atenção / 🟢 Pode esperar. "
        "Inclua uma sugestão de próximo passo para cada item.\n\n"
        "Seja direto e objetivo. Use listas quando possível.\n\n"
        f"Mensagens dos canais:\n{raw}"
    )

    try:
        result = await _call_claude(prompt, max_tokens=3000)
    except anthropic.APIError as e:
        await status.edit(content=f"Erro ao contatar o Claude: {e}")
        return

    await status.delete()
    await _send_long(ctx.channel, f"**🗂️ Briefing — {user_ref}**\n\n{result}")


@bot.command(name="historico")
async def cmd_historico(ctx: commands.Context):
    """Mostra quantas mensagens estão no histórico de conversa do canal."""
    count = len(conversation_history[ctx.channel.id])
    await ctx.send(f"Há {count} mensagem(ns) no histórico deste canal.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
