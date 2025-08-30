# Bloop v0.1 — Economy + Games + Server Features
# pip install -U discord.py

import os
import asyncio
import random
import sqlite3
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

# -------------------------
# CONFIG
# -------------------------
COMMAND_PREFIX = "!"
DAILY_AMOUNT = 100
RANDOM_MONEY_MAX = 50
LAMBO_GIF = "https://tenor.com/uxxicB3aCSs.gif"  # fun win gif
DEFAULT_CURRENCY = "Bloop Coins"
JOIN_WINDOW_SECONDS = 25  # for multiplayer dice
GAMBLE_COOLDOWN_SECONDS = 5
RANDOM_MONEY_COOLDOWN_MIN = 2

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
tree = bot.tree

DB_PATH = "bloop.sqlite3"
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# -------------------------
# DATABASE
# -------------------------
def db_setup():
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        guild_id INTEGER,
        user_id INTEGER,
        balance INTEGER DEFAULT 0,
        last_daily TEXT,
        badges TEXT DEFAULT '',
        PRIMARY KEY(guild_id, user_id)
    );
    """)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS servers(
        guild_id INTEGER PRIMARY KEY,
        currency_name TEXT DEFAULT '{DEFAULT_CURRENCY}',
        debt INTEGER DEFAULT 0,
        treasury INTEGER DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS loans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        lender_id INTEGER,
        borrower_id INTEGER,
        amount INTEGER,
        status TEXT, -- pending, accepted, rejected
        created_at TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cooldowns(
        guild_id INTEGER,
        user_id INTEGER,
        name TEXT,
        next_time TEXT,
        PRIMARY KEY(guild_id, user_id, name)
    );
    """)
    conn.commit()

def get_currency(guild_id: int) -> str:
    cur.execute("SELECT currency_name FROM servers WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else DEFAULT_CURRENCY

def ensure_server_row(guild_id: int):
    cur.execute("INSERT OR IGNORE INTO servers(guild_id) VALUES(?)", (guild_id,))
    conn.commit()

def ensure_user_row(guild_id: int, user_id: int):
    cur.execute("INSERT OR IGNORE INTO users(guild_id, user_id) VALUES(?,?)", (guild_id, user_id))
    conn.commit()

def add_balance(guild_id: int, user_id: int, delta: int):
    ensure_user_row(guild_id, user_id)
    cur.execute("UPDATE users SET balance = COALESCE(balance,0) + ? WHERE guild_id=? AND user_id=?", (delta, guild_id, user_id))
    conn.commit()

def get_balance(guild_id: int, user_id: int) -> int:
    ensure_user_row(guild_id, user_id)
    cur.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    return int(row[0]) if row else 0

def set_cooldown(guild_id: int, user_id: int, name: str, seconds: int):
    next_time = (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()
    cur.execute("""
        INSERT INTO cooldowns(guild_id, user_id, name, next_time)
        VALUES(?,?,?,?)
        ON CONFLICT(guild_id, user_id, name) DO UPDATE SET next_time=excluded.next_time
    """, (guild_id, user_id, name, next_time))
    conn.commit()

def check_cooldown(guild_id: int, user_id: int, name: str):
    cur.execute("SELECT next_time FROM cooldowns WHERE guild_id=? AND user_id=? AND name=?",
                (guild_id, user_id, name))
    row = cur.fetchone()
    if not row or not row[0]:
        return True, 0
    nt = datetime.fromisoformat(row[0])
    now = datetime.utcnow()
    if now >= nt:
        return True, 0
    remaining = int((nt - now).total_seconds())
    return False, remaining

db_setup()

# -------------------------
# UTILS
# -------------------------
def is_adminish(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.manage_roles

def fmt(amount: int, currency: str) -> str:
    return f"{amount:,} {currency}"

async def send_win_gif(channel: discord.TextChannel, note: str = "You won!"):
    embed = discord.Embed(title="🏎️ BIG WIN!", description=note, color=discord.Color.gold())
    embed.set_image(url=LAMBO_GIF)
    await channel.send(embed=embed)

# -------------------------
# VIEWS / INTERACTIONS
# -------------------------
class EconomySetupModal(discord.ui.Modal, title="Set Up Server Economy"):
    currency_name = discord.ui.TextInput(label="Currency name", placeholder="e.g., Banana Bucks", max_length=24)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        ensure_server_row(self.guild_id)
        name = self.currency_name.value.strip() or DEFAULT_CURRENCY
        cur.execute("UPDATE servers SET currency_name=? WHERE guild_id=?", (name, self.guild_id))
        conn.commit()
        await interaction.response.send_message(f"✅ Server currency set to **{name}**.", ephemeral=True)

class GamesMenu(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id

    @discord.ui.select(
        placeholder="Choose a game…",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="💸 Random Money", description="Win 0–50 instantly (cooldown).", value="random"),
            discord.SelectOption(label="🎲 Dice (Multiplayer)", description="Join pot, highest roll wins!", value="dice"),
            discord.SelectOption(label="❌⭕ Tic Tac Toe", description="Challenge a friend with O/X.", value="ttt"),
            discord.SelectOption(label="🪙 Coin Toss", description="Heads or Tails (bet).", value="coin"),
            discord.SelectOption(label="🎡 Spinning Wheel", description="Spin for multipliers (bet).", value="wheel"),
            discord.SelectOption(label="♠️♣️♥️♦️ Blackjack", description="Beat the dealer at 21!", value="blackjack"),
        ]
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Only the command invoker can use this menu.", ephemeral=True)
        choice = select.values[0]
        await interaction.response.send_message(f"Use `{COMMAND_PREFIX}bloopplay {choice}` to start!", ephemeral=True)

# -------------------------
# GAMES STATE (in-memory)
# -------------------------
dice_sessions = {}  # channel_id -> session dict
gamble_cooldowns = {}  # (guild_id,user_id) -> datetime

# -------------------------
# BOT EVENTS
# -------------------------
@bot.event
async def on_ready():
    print(f"Bloop is online as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"/ commands synced: {len(synced)}")
    except Exception as e:
        print("Slash sync failed:", e)

# -------------------------
# HELP
# -------------------------
@bot.command(name="bloophelp")
async def bloophelp(ctx: commands.Context):
    currency = get_currency(ctx.guild.id)
    embed = discord.Embed(title="🐙 Bloop Help", color=discord.Color.blurple())
    embed.description = (
        f"**💰 Economy**\n"
        f"`{COMMAND_PREFIX}bloopbank` – Your balance\n"
        f"`{COMMAND_PREFIX}bloopdaily` – Claim daily {currency}\n"
        f"`{COMMAND_PREFIX}bloopgift @user amount` – Gift coins\n"
        f"`{COMMAND_PREFIX}bloopboard` – Top 10 richest\n"
        f"`{COMMAND_PREFIX}economy` – Setup server economy (admin)\n"
        f"`{COMMAND_PREFIX}trade <target_server_id> <amount>` – Server → server transfer (admin)\n"
        f"`{COMMAND_PREFIX}borrow @user <amount>` – Ask user for a loan\n\n"
        f"**🎮 Games**\n"
        f"`{COMMAND_PREFIX}bloopgames` – Pick a game\n"
        f"`{COMMAND_PREFIX}bloopplay random` – Random money 💸\n"
        f"`{COMMAND_PREFIX}bloopplay dice <bet>` – Multiplayer dice 🎲\n"
        f"`{COMMAND_PREFIX}bloopplay ttt @opponent` – Tic Tac Toe ❌⭕\n"
        f"`{COMMAND_PREFIX}bloopplay coin <bet> <heads/tails>` – Coin toss 🪙\n"
        f"`{COMMAND_PREFIX}bloopplay wheel <bet>` – Spinning wheel 🎡\n"
        f"`{COMMAND_PREFIX}bloopplay blackjack <bet>` – Blackjack ♠️♣️♥️♦️\n\n"
        f"**🔧 Misc**\n"
        f"`{COMMAND_PREFIX}bloopcheck` – Is Bloop alive?\n"
        f"`{COMMAND_PREFIX}pong` – Bloop says ping\n"
        f"`/poll` – Create a poll (slash command)"
    )
    await ctx.send(embed=embed)

# -------------------------
# BASIC PING
# -------------------------
@bot.command(name="bloopcheck")
async def bloopcheck(ctx):
    await ctx.send("Bloop is here! 🫧 https://tenor.com/uxxicB3aCSs.gif")

@bot.command(name="pong")
async def pong(ctx):
    await ctx.send("ping!")

# -------------------------
# ECONOMY CORE
# -------------------------
@bot.command(name="bloopbank")
async def bloopbank(ctx, member: discord.Member = None):
    member = member or ctx.author
    bal = get_balance(ctx.guild.id, member.id)
    currency = get_currency(ctx.guild.id)
    embed = discord.Embed(title="🏦 Bloop Bank", color=discord.Color.green())
    embed.add_field(name=str(member), value=f"Balance: **{fmt(bal, currency)}**", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="bloopdaily")
async def bloopdaily(ctx):
    ensure_user_row(ctx.guild.id, ctx.author.id)
    # cooldown: 24h
    ok, rem = check_cooldown(ctx.guild.id, ctx.author.id, "daily")
    if not ok:
        hours = rem // 3600
        mins = (rem % 3600) // 60
        return await ctx.send(f"⏳ You can claim again in **{hours}h {mins}m**.")
    add_balance(ctx.guild.id, ctx.author.id, DAILY_AMOUNT)
    set_cooldown(ctx.guild.id, ctx.author.id, "daily", 24*3600)
    currency = get_currency(ctx.guild.id)
    await ctx.send(f"🎁 You claimed **{fmt(DAILY_AMOUNT, currency)}**!")

@bot.command(name="bloopgift")
async def bloopgift(ctx, member: discord.Member = None, amount: int = None):
    if member is None or amount is None or amount <= 0:
        return await ctx.send(f"Usage: `{COMMAND_PREFIX}bloopgift @user amount`")
    if member.bot:
        return await ctx.send("You can’t gift bots.")
    guild_id = ctx.guild.id
    sender_bal = get_balance(guild_id, ctx.author.id)
    if sender_bal < amount:
        return await ctx.send("❌ Not enough balance.")
    add_balance(guild_id, ctx.author.id, -amount)
    add_balance(guild_id, member.id, amount)
    currency = get_currency(guild_id)
    await ctx.send(f"🔄 {ctx.author.mention} sent **{fmt(amount, currency)}** to {member.mention}!")

@bot.command(name="bloopboard")
async def bloopboard(ctx):
    currency = get_currency(ctx.guild.id)
    cur.execute("SELECT user_id, balance FROM users WHERE guild_id=? ORDER BY balance DESC LIMIT 10", (ctx.guild.id,))
    rows = cur.fetchall()
    if not rows:
        return await ctx.send("No data yet.")
    desc = []
    for i, (uid, bal) in enumerate(rows, start=1):
        user = ctx.guild.get_member(uid) or f"<@{uid}>"
        name = user.display_name if isinstance(user, discord.Member) else str(user)
        desc.append(f"**{i}.** {name} — {fmt(int(bal), currency)}")
    embed = discord.Embed(title=f"🏆 Richest in {ctx.guild.name}", description="\n".join(desc), color=discord.Color.gold())
    await ctx.send(embed=embed)

# -------------------------
# SERVER ECONOMY SETUP + ADMIN
# -------------------------
@bot.command(name="economy")
async def economy(ctx, *, currency_name: str = None):
    if not is_adminish(ctx.author):
        return await ctx.send("Only server owner/managers/admins can use this.")
    ensure_server_row(ctx.guild.id)
    if currency_name:
        cur.execute("UPDATE servers SET currency_name=? WHERE guild_id=?", (currency_name[:24], ctx.guild.id))
        conn.commit()
        return await ctx.send(f"✅ Server currency set to **{currency_name[:24]}**.")
    # interactive modal
    try:
        await ctx.send("Opening setup… (modal)", delete_after=2)
        await ctx.send_modal(EconomySetupModal(ctx.guild.id))  # requires discord.py 2.4+
    except Exception:
        await ctx.send("Your discord.py might be older. Provide a currency name: `!economy My Coins`")

@bot.command(name="trade")
async def server_trade(ctx, target_guild_id: int = None, amount: int = None):
    if not is_adminish(ctx.author):
        return await ctx.send("Only server owner/managers/admins can use this.")
    if not target_guild_id or not amount or amount <= 0:
        return await ctx.send(f"Usage: `{COMMAND_PREFIX}trade <target_server_id> <amount>`")
    # For prototype, subtract from THIS server treasury and add to target treasury.
    ensure_server_row(ctx.guild.id)
    ensure_server_row(target_guild_id)
    # make sure this server has enough treasury (or allow negative? your spec allows loans elsewhere)
    cur.execute("SELECT treasury FROM servers WHERE guild_id=?", (ctx.guild.id,))
    src_treas = cur.fetchone()[0] or 0
    if src_treas < amount:
        return await ctx.send("❌ Not enough funds in this server treasury.")
    cur.execute("UPDATE servers SET treasury=treasury-? WHERE guild_id=?", (amount, ctx.guild.id))
    cur.execute("UPDATE servers SET treasury=treasury+? WHERE guild_id=?", (amount, target_guild_id))
    conn.commit()
    await ctx.send(f"🏦 Transferred **{amount:,}** treasury units to server `{target_guild_id}`.")

@bot.command(name="borrow")
async def borrow(ctx, member: discord.Member = None, amount: int = None):
    if member is None or amount is None or amount <= 0:
        return await ctx.send(f"Usage: `{COMMAND_PREFIX}borrow @user <amount>`")
    guild_id = ctx.guild.id
    cur.execute("INSERT INTO loans(guild_id, lender_id, borrower_id, amount, status, created_at) VALUES(?,?,?,?,?,?)",
                (guild_id, member.id, ctx.author.id, amount, "pending", datetime.utcnow().isoformat()))
    loan_id = cur.lastrowid
    conn.commit()

    view = discord.ui.View()
    async def accept(interaction: discord.Interaction):
        if interaction.user.id != member.id:
            return await interaction.response.send_message("Only the lender can accept.", ephemeral=True)
        # check lender balance
        if get_balance(guild_id, member.id) < amount:
            return await interaction.response.send_message("❌ Not enough balance to loan.", ephemeral=True)
        add_balance(guild_id, member.id, -amount)
        add_balance(guild_id, ctx.author.id, amount)
        cur.execute("UPDATE loans SET status='accepted' WHERE id=?", (loan_id,))
        conn.commit()
        await interaction.response.edit_message(content=f"✅ Loan accepted. {member.mention} → {ctx.author.mention}: {amount:,}", view=None)

    async def reject(interaction: discord.Interaction):
        if interaction.user.id != member.id:
            return await interaction.response.send_message("Only the lender can reject.", ephemeral=True)
        cur.execute("UPDATE loans SET status='rejected' WHERE id=?", (loan_id,))
        conn.commit()
        await interaction.response.edit_message(content=f"❌ Loan rejected by {member.mention}.", view=None)

    view.add_item(discord.ui.Button(label="Accept", style=discord.ButtonStyle.success))
    view.add_item(discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger))
    # bind callbacks
    view.children[0].callback = accept
    view.children[1].callback = reject

    currency = get_currency(guild_id)
    await ctx.send(f"💸 {member.mention}, {ctx.author.mention} requests a loan of **{fmt(amount, currency)}**.", view=view)

# -------------------------
# BLOOP GAMES MENU
# -------------------------
@bot.command(name="bloopgames")
async def bloopgames(ctx):
    embed = discord.Embed(title="🎮 Bloop Games", description="Pick a game from the menu below, then run `!bloopplay <game>`.", color=discord.Color.purple())
    embed.add_field(name="Available", value="`random`, `dice`, `ttt`, `coin`, `wheel`, `blackjack`", inline=False)
    await ctx.send(embed=embed, view=GamesMenu(ctx.author.id))

@bot.command(name="bloopplay")
async def bloopplay(ctx, game: str = None, *args):
    if not game:
        return await ctx.send("❌ no game available")
    game = game.lower()

    if game == "random":
        # simple RNG earn with cooldown
        ok, rem = check_cooldown(ctx.guild.id, ctx.author.id, "random_money")
        if not ok:
            return await ctx.send(f"⏳ Try again in {rem}s.")
        amount = random.randint(0, RANDOM_MONEY_MAX)
        add_balance(ctx.guild.id, ctx.author.id, amount)
        set_cooldown(ctx.guild.id, ctx.author.id, "random_money", RANDOM_MONEY_COOLDOWN_MIN*60)
        currency = get_currency(ctx.guild.id)
        return await ctx.send(f"🎁 You found **{fmt(amount, currency)}** on the ground.")

    elif game == "dice":
        # args: bet
        if len(args) < 1 or not args[0].isdigit():
            return await ctx.send(f"Usage: `{COMMAND_PREFIX}bloopplay dice <bet>`")
        bet = int(args[0])
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        bal = get_balance(ctx.guild.id, ctx.author.id)
        if bal < bet:
            return await ctx.send("❌ Not enough balance for that bet.")
        ch_id = ctx.channel.id
        if ch_id in dice_sessions:
            return await ctx.send("A dice game is already running in this channel. Please wait.")

        # create session
        dice_sessions[ch_id] = {
            "guild_id": ctx.guild.id,
            "bets": {ctx.author.id: bet},
            "message_id": None,
            "started_at": datetime.utcnow()
        }
        add_balance(ctx.guild.id, ctx.author.id, -bet)
        currency = get_currency(ctx.guild.id)

        view = discord.ui.View(timeout=JOIN_WINDOW_SECONDS)

        async def join(interaction: discord.Interaction):
            if interaction.channel_id != ch_id:
                return
            uid = interaction.user.id
            if uid in dice_sessions[ch_id]["bets"]:
                return await interaction.response.send_message("You already joined.", ephemeral=True)
            # ask for same bet as starter?
            # Let each choose their own bet (deduct now)
            user_bal = get_balance(ctx.guild.id, uid)
            if user_bal < bet:
                return await interaction.response.send_message("Not enough balance for the entry bet.", ephemeral=True)
            add_balance(ctx.guild.id, uid, -bet)
            dice_sessions[ch_id]["bets"][uid] = bet
            await interaction.response.edit_message(content=f"🎲 **Bloop Dice** started by {ctx.author.mention}\n"
                                                           f"Players joined: {len(dice_sessions[ch_id]['bets'])}\n"
                                                           f"Entry bet: **{fmt(bet, currency)}**\n"
                                                           f"Join window: {JOIN_WINDOW_SECONDS}s", view=view)

        join_btn = discord.ui.Button(label="Join Dice", style=discord.ButtonStyle.primary, emoji="🎲")
        join_btn.callback = join
        view.add_item(join_btn)

        msg = await ctx.send(f"🎲 **Bloop Dice** started by {ctx.author.mention}\n"
                             f"Players joined: 1\n"
                             f"Entry bet: **{fmt(bet, currency)}**\n"
                             f"Join window: {JOIN_WINDOW_SECONDS}s", view=view)
        dice_sessions[ch_id]["message_id"] = msg.id

        await asyncio.sleep(JOIN_WINDOW_SECONDS)
        # evaluate
        sess = dice_sessions.pop(ch_id, None)
        if not sess:
            return
        players = list(sess["bets"].keys())
        if len(players) < 2:
            # refund starter
            add_balance(ctx.guild.id, players[0], bet)
            return await ctx.send("Not enough players joined. Bet refunded.")
        rolls = {uid: random.randint(1, 6) for uid in players}
        high = max(rolls.values())
        winners = [u for u, r in rolls.items() if r == high]
        pot = sum(sess["bets"].values())
        prize_each = pot // len(winners)
        for w in winners:
            add_balance(ctx.guild.id, w, prize_each)
        lines = [f"<@{uid}> rolled **{r}**" for uid, r in rolls.items()]
        await ctx.send("🎲 Rolls:\n" + "\n".join(lines))
        if len(winners) == 1:
            await send_win_gif(ctx.channel, note=f"<@{winners[0]}> won the pot: **{fmt(pot, currency)}**!")
        else:
            await ctx.send(f"🤝 Tie! Winners split pot **{fmt(pot, currency)}** → {', '.join(f'<@{w}>' for w in winners)}")

    elif game == "coin":
        # coin toss bet heads/tails
        if len(args) < 2:
            return await ctx.send(f"Usage: `{COMMAND_PREFIX}bloopplay coin <bet> <heads|tails>`")
        try:
            bet = int(args[0])
        except:
            return await ctx.send("Bet must be a number.")
        pick = args[1].lower()
        if pick not in ("heads", "tails"):
            return await ctx.send("Pick heads or tails.")
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        bal = get_balance(ctx.guild.id, ctx.author.id)
        if bal < bet:
            return await ctx.send("❌ Not enough balance.")
        # cooldown per user
        key = (ctx.guild.id, ctx.author.id)
        now = datetime.utcnow()
        if key in gamble_cooldowns and (now - gamble_cooldowns[key]).total_seconds() < GAMBLE_COOLDOWN_SECONDS:
            return await ctx.send("⏳ Slow down a bit!")
        gamble_cooldowns[key] = now

        add_balance(ctx.guild.id, ctx.author.id, -bet)
        result = random.choice(["heads", "tails"])
        currency = get_currency(ctx.guild.id)
        if result == pick:
            add_balance(ctx.guild.id, ctx.author.id, bet * 2)
            await send_win_gif(ctx.channel, note=f"You won **{fmt(bet*2, currency)}** (coin was **{result}**)!")
        else:
            await ctx.send(f"😬 Lost. It was **{result}**.")

    elif game == "wheel":
        # spinning wheel: random multiplier
        if len(args) < 1:
            return await ctx.send(f"Usage: `{COMMAND_PREFIX}bloopplay wheel <bet>`")
        try:
            bet = int(args[0])
        except:
            return await ctx.send("Bet must be a number.")
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        bal = get_balance(ctx.guild.id, ctx.author.id)
        if bal < bet:
            return await ctx.send("❌ Not enough balance.")
        add_balance(ctx.guild.id, ctx.author.id, -bet)
        # multipliers with rough probabilities
        wheel = [
            (0, 0.20),
            (0.5, 0.30),
            (1, 0.25),
            (2, 0.15),
            (5, 0.08),
            (10, 0.02),
        ]
        r = random.random()
        acc = 0
        mult = 0
        for m, p in wheel:
            acc += p
            if r <= acc:
                mult = m
                break
        winnings = int(bet * mult)
        if winnings > 0:
            add_balance(ctx.guild.id, ctx.author.id, winnings)
            currency = get_currency(ctx.guild.id)
            await send_win_gif(ctx.channel, note=f"Wheel landed **x{mult}** → You got **{fmt(winnings, currency)}**!")
        else:
            await ctx.send("💀 Wheel landed on **x0** — better luck next time.")

    elif game == "ttt":
        # Tic Tac Toe vs mentioned user
        if not args or not ctx.message.mentions:
            return await ctx.send(f"Usage: `{COMMAND_PREFIX}bloopplay ttt @opponent`")
        opponent = ctx.message.mentions[0]
        if opponent.bot or opponent.id == ctx.author.id:
            return await ctx.send("Pick a real opponent.")
        await start_ttt(ctx, ctx.author, opponent)

    elif game == "blackjack":
        # Blackjack vs dealer
        if len(args) < 1:
            return await ctx.send(f"Usage: `{COMMAND_PREFIX}bloopplay blackjack <bet>`")
        try:
            bet = int(args[0])
        except:
            return await ctx.send("Bet must be a number.")
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        bal = get_balance(ctx.guild.id, ctx.author.id)
        if bal < bet:
            return await ctx.send("❌ Not enough balance.")

        # cooldown per user
        key = (ctx.guild.id, ctx.author.id)
        now = datetime.utcnow()
        if key in gamble_cooldowns and (now - gamble_cooldowns[key]).total_seconds() < GAMBLE_COOLDOWN_SECONDS:
            return await ctx.send("⏳ Slow down a bit!")
        gamble_cooldowns[key] = now

        add_balance(ctx.guild.id, ctx.author.id, -bet)
        await start_blackjack(ctx, bet)

    else:
        await ctx.send("❌ no game available")

# -------------------------
# TIC TAC TOE GAME
# -------------------------
class TTTView(discord.ui.View):
    def __init__(self, ctx, player_x: discord.Member, player_o: discord.Member, reward:int=25):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.px = player_x
        self.po = player_o
        self.turn = self.px.id  # X starts
        self.board = [" "] * 9
        self.finished = False
        self.reward = reward

        for i in range(9):
            btn = discord.ui.Button(label="⬜", style=discord.ButtonStyle.secondary, row=i//3, custom_id=str(i))
            btn.callback = self.make_move
            self.add_item(btn)

    def create_embed(self, status_text: str = None):
        embed = discord.Embed(title="❌⭕ Tic Tac Toe", color=discord.Color.blue())

        # Create visual board
        board_display = ""
        for i in range(3):
            row = ""
            for j in range(3):
                idx = i * 3 + j
                if self.board[idx] == "X":
                    row += "❌"
                elif self.board[idx] == "O":
                    row += "⭕"
                else:
                    row += "⬜"
                if j < 2:
                    row += " "
            board_display += row + "\n"

        embed.add_field(name="Game Board", value=board_display, inline=False)
        embed.add_field(name="Players", value=f"❌ {self.px.mention}\n⭕ {self.po.mention}", inline=True)

        if not self.finished:
            current_player = self.px if self.turn == self.px.id else self.po
            current_symbol = "❌" if self.turn == self.px.id else "⭕"
            embed.add_field(name="Current Turn", value=f"{current_symbol} {current_player.mention}", inline=True)

        if status_text:
            embed.add_field(name="Status", value=status_text, inline=False)

        return embed

    async def make_move(self, interaction: discord.Interaction):
        if self.finished:
            return
        if interaction.user.id not in (self.px.id, self.po.id):
            return await interaction.response.send_message("You are not in this game.", ephemeral=True)
        if interaction.user.id != self.turn:
            return await interaction.response.send_message("Not your turn.", ephemeral=True)
        idx = int(interaction.data["custom_id"])
        if self.board[idx] != " ":
            return await interaction.response.send_message("That spot is taken.", ephemeral=True)

        mark = "X" if self.turn == self.px.id else "O"
        self.board[idx] = mark
        # update button
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == str(idx):
                c.label = "❌" if mark == "X" else "⭕"
                c.style = discord.ButtonStyle.success if mark == "X" else discord.ButtonStyle.danger
                c.disabled = True
                break

        winner = self.check_winner()
        if winner or " " not in self.board:
            self.finished = True
            for c in self.children:
                if isinstance(c, discord.ui.Button):
                    c.disabled = True

            if winner:
                win_user = self.px if winner == "X" else self.po
                currency = get_currency(self.ctx.guild.id)
                add_balance(self.ctx.guild.id, win_user.id, self.reward)
                status = f"🏆 {win_user.mention} wins **{fmt(self.reward, currency)}**!"
            else:
                status = "🤝 It's a draw!"

            embed = self.create_embed(status)
            await interaction.response.edit_message(embed=embed, view=self)
            return

        # swap turn
        self.turn = self.po.id if self.turn == self.px.id else self.px.id
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def check_winner(self):
        wins = [
            (0,1,2),(3,4,5),(6,7,8),
            (0,3,6),(1,4,7),(2,5,8),
            (0,4,8),(2,4,6)
        ]
        for a,b,c in wins:
            if self.board[a] != " " and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

# -------------------------
# BLACKJACK GAME
# -------------------------
def create_deck():
    suits = ['♠️', '♣️', '♥️', '♦️']
    ranks = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
    deck = []
    for suit in suits:
        for rank in ranks:
            deck.append(f"{rank}{suit}")
    random.shuffle(deck)
    return deck

def card_value(card):
    rank = card[:-2] if card[:-2] in ['10'] else card[:-2]
    if rank in ['J', 'Q', 'K']:
        return 10
    elif rank == 'A':
        return 11  # Will handle aces later
    else:
        return int(rank)

def hand_value(hand):
    value = sum(card_value(card) for card in hand)
    aces = sum(1 for card in hand if card[:-2] == 'A')

    # Convert aces from 11 to 1 if needed
    while value > 21 and aces > 0:
        value -= 10
        aces -= 1

    return value

def format_hand(hand, hide_first=False):
    if hide_first:
        return f"🃏 {' '.join(hand[1:])}"
    return ' '.join(hand)

class BlackjackView(discord.ui.View):
    def __init__(self, ctx, bet: int):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.deck = create_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
        self.finished = False

    @discord.ui.button(label="🎯 Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("This isn't your game!", ephemeral=True)

        if self.finished:
            return

        self.player_hand.append(self.deck.pop())
        player_val = hand_value(self.player_hand)

        if player_val > 21:
            # Bust
            self.finished = True
            for child in self.children:
                child.disabled = True

            embed = discord.Embed(title="♠️♣️♥️♦️ Blackjack", color=discord.Color.red())
            embed.add_field(name="🎯 Your Hand", value=f"{format_hand(self.player_hand)} = **{player_val}**", inline=False)
            embed.add_field(name="🏦 Dealer Hand", value=f"{format_hand(self.dealer_hand)} = **{hand_value(self.dealer_hand)}**", inline=False)
            embed.add_field(name="Result", value="💥 **BUST!** You lose!", inline=False)

            await interaction.response.edit_message(embed=embed, view=self)
        else:
            embed = discord.Embed(title="♠️♣️♥️♦️ Blackjack", color=discord.Color.blue())
            embed.add_field(name="🎯 Your Hand", value=f"{format_hand(self.player_hand)} = **{player_val}**", inline=False)
            embed.add_field(name="🏦 Dealer Hand", value=f"{format_hand(self.dealer_hand, hide_first=True)} = **?**", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🛑 Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("This isn't your game!", ephemeral=True)

        if self.finished:
            return

        self.finished = True
        for child in self.children:
            child.disabled = True

        # Dealer plays
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_val = hand_value(self.player_hand)
        dealer_val = hand_value(self.dealer_hand)

        # Determine winner
        currency = get_currency(self.ctx.guild.id)
        if dealer_val > 21:
            # Dealer bust, player wins
            add_balance(self.ctx.guild.id, self.ctx.author.id, self.bet * 2)
            result = f"🎉 **YOU WIN!** Dealer busted! +{fmt(self.bet * 2, currency)}"
            color = discord.Color.green()
        elif player_val > dealer_val:
            # Player wins
            add_balance(self.ctx.guild.id, self.ctx.author.id, self.bet * 2)
            result = f"🎉 **YOU WIN!** +{fmt(self.bet * 2, currency)}"
            color = discord.Color.green()
        elif player_val == dealer_val:
            # Push (tie)
            add_balance(self.ctx.guild.id, self.ctx.author.id, self.bet)
            result = f"🤝 **PUSH!** It's a tie! +{fmt(self.bet, currency)}"
            color = discord.Color.orange()
        else:
            # Dealer wins
            result = "😔 **DEALER WINS!** You lose!"
            color = discord.Color.red()

        embed = discord.Embed(title="♠️♣️♥️♦️ Blackjack", color=color)
        embed.add_field(name="🎯 Your Hand", value=f"{format_hand(self.player_hand)} = **{player_val}**", inline=False)
        embed.add_field(name="🏦 Dealer Hand", value=f"{format_hand(self.dealer_hand)} = **{dealer_val}**", inline=False)
        embed.add_field(name="Result", value=result, inline=False)

        await interaction.response.edit_message(embed=embed, view=self)

async def start_blackjack(ctx, bet: int):
    view = BlackjackView(ctx, bet)

    # Check for natural blackjack
    player_val = hand_value(view.player_hand)
    dealer_val = hand_value(view.dealer_hand)

    if player_val == 21:
        # Player blackjack
        view.finished = True
        for child in view.children:
            child.disabled = True

        currency = get_currency(ctx.guild.id)
        if dealer_val == 21:
            # Both blackjack, push
            add_balance(ctx.guild.id, ctx.author.id, bet)
            result = f"🤝 **BLACKJACK PUSH!** Both got 21! +{fmt(bet, currency)}"
            color = discord.Color.orange()
        else:
            # Player blackjack wins
            winnings = int(bet * 2.5)  # Blackjack pays 3:2
            add_balance(ctx.guild.id, ctx.author.id, winnings)
            result = f"🃏 **BLACKJACK!** +{fmt(winnings, currency)}"
            color = discord.Color.gold()

        embed = discord.Embed(title="♠️♣️♥️♦️ Blackjack", color=color)
        embed.add_field(name="🎯 Your Hand", value=f"{format_hand(view.player_hand)} = **{player_val}**", inline=False)
        embed.add_field(name="🏦 Dealer Hand", value=f"{format_hand(view.dealer_hand)} = **{dealer_val}**", inline=False)
        embed.add_field(name="Result", value=result, inline=False)

        await ctx.send(embed=embed, view=view)
    else:
        # Normal game
        embed = discord.Embed(title="♠️♣️♥️♦️ Blackjack", color=discord.Color.blue())
        embed.add_field(name="🎯 Your Hand", value=f"{format_hand(view.player_hand)} = **{player_val}**", inline=False)
        embed.add_field(name="🏦 Dealer Hand", value=f"{format_hand(view.dealer_hand, hide_first=True)} = **?**", inline=False)

        await ctx.send(embed=embed, view=view)

async def start_ttt(ctx, p1: discord.Member, p2: discord.Member):
    view = TTTView(ctx, p1, p2)
    embed = view.create_embed()
    await ctx.send(embed=embed, view=view)

# -------------------------
# /POLL SLASH COMMAND
# -------------------------
@tree.command(name="poll", description="Create a quick poll")
@app_commands.describe(question="What to ask", option1="Option 1", option2="Option 2", option3="Option 3 (optional)", option4="Option 4 (optional)")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str, option3: str = None, option4: str = None):
    options = [o for o in [option1, option2, option3, option4] if o]
    if len(options) < 2:
        return await interaction.response.send_message("Provide at least 2 options.", ephemeral=True)
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    lines = [f"{emojis[i]} {opt}" for i, opt in enumerate(options)]
    embed = discord.Embed(title="📊 Poll", description=f"**{question}**\n\n" + "\n".join(lines), color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])




# --- KEEP ALIVE SECTION ---


from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route("/")
def home():
    return "Bloop is alive!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()



# -------------------------
# RUN
# -------------------------
bot.run(os.getenv("DISCORD_TOKEN"))