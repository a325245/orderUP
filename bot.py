import os
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import base64
import sqlite3
import re
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# ==========================================
# CONFIGURATION & DATABASE
# ==========================================
CRAFTER_ROLE_NAME = "Crafter"
XIVAPI_BASE = "https://v2.xivapi.com/api"

# Lock file paths to the exact folder this python script is inside
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "orders.db")

# Read the bot token from the .env file
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print(f"❌ ERROR: Could not find DISCORD_TOKEN in your .env file!")
    exit(1)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            control_message_id INTEGER PRIMARY KEY,
            root_message_id INTEGER,
            requester_id INTEGER,
            crafter_id INTEGER DEFAULT NULL,
            status TEXT DEFAULT 'Pending',
            items_summary TEXT,
            recipient TEXT,
            tc_url TEXT
        )
    """)
    conn.commit()
    conn.close()

async def rebuild_database():
    """Scans Discord channels on startup to rebuild the database if it was wiped by Render."""
    print("🔄 Scanning Discord to rebuild the database...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for guild in bot.guilds:
        # --- 1. REBUILD OPEN ORDERS ---
        open_channel = discord.utils.get(guild.text_channels, name="open-orders")
        if open_channel:
            for thread in open_channel.threads:
                try:
                    # Find the control message (the one with the buttons)
                    control_msg_id = None
                    async for msg in thread.history(limit=5, oldest_first=True):
                        if msg.author == bot.user and msg.components:
                            control_msg_id = msg.id
                            break
                    if not control_msg_id: continue
                    
                    # Check if already in DB
                    cursor.execute("SELECT 1 FROM orders WHERE control_message_id=?", (control_msg_id,))
                    if cursor.fetchone(): continue
                    
                    # The thread's ID is the exact same as the root message's ID
                    root_msg = await open_channel.fetch_message(thread.id)
                    if not root_msg.embeds: continue
                    embed = root_msg.embeds[0]
                    
                    recipient, items_summary, tc_url = "", "", None
                    requester_id, crafter_id = 0, None
                    status = 'Claimed' if "Claimed" in embed.title else 'Pending'
                    
                    for field in embed.fields:
                        if field.name == "Recipient Target": recipient = field.value
                        elif field.name in ["Requested Items", "Requested Order"]: items_summary = field.value
                        elif field.name == "Requested By":
                            match = re.search(r'\d+', field.value)
                            if match: requester_id = int(match.group())
                        elif field.name == "Teamcraft Link":
                            match = re.search(r'\]\((.*?)\)', field.value)
                            if match: tc_url = match.group(1)
                        elif field.name == "Claimed By":
                            match = re.search(r'\d+', field.value)
                            if match: crafter_id = int(match.group())
                            
                    cursor.execute("""
                        INSERT INTO orders (control_message_id, root_message_id, requester_id, crafter_id, status, items_summary, recipient, tc_url) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (control_msg_id, root_msg.id, requester_id, crafter_id, status, items_summary, recipient, tc_url))
                    conn.commit()
                except Exception as e:
                    print(f"⚠️ Failed to recover thread {thread.name}: {e}")

        # --- 2. REBUILD CLOSED ORDERS (Last 50 for Restore button functionality) ---
        closed_channel = discord.utils.get(guild.text_channels, name="closed-orders")
        if closed_channel:
            async for msg in closed_channel.history(limit=50):
                if msg.author == bot.user and msg.embeds:
                    embed = msg.embeds[0]
                    footer_text = embed.footer.text or ""
                    if footer_text.startswith("Order ID: "):
                        control_msg_id = int(footer_text.replace("Order ID: ", ""))
                        
                        cursor.execute("SELECT 1 FROM orders WHERE control_message_id=?", (control_msg_id,))
                        if cursor.fetchone(): continue
                        
                        recipient, items_summary, tc_url = "", "", None
                        requester_id = 0
                        
                        for field in embed.fields:
                            if field.name == "Recipient Target": recipient = field.value
                            elif field.name in ["Requested Items", "Requested Order"]: items_summary = field.value
                            elif field.name == "Requested By":
                                match = re.search(r'\d+', field.value)
                                if match: requester_id = int(match.group())
                            elif field.name == "Teamcraft Link":
                                match = re.search(r'\]\((.*?)\)', field.value)
                                if match: tc_url = match.group(1)
                                
                        cursor.execute("""
                            INSERT INTO orders (control_message_id, root_message_id, requester_id, status, items_summary, recipient, tc_url) 
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (control_msg_id, 0, requester_id, "Completed", items_summary, recipient, tc_url))
                        conn.commit()

    conn.close()
    print("✅ Database successfully synced with Discord!")

def generate_teamcraft_url(teamcraft_payload):
    # (item_id, null, quantity) required by Teamcraft
    import_str = ";".join([f"{item_id},null,{qty}" for item_id, qty in teamcraft_payload])
    encoded = base64.b64encode(import_str.encode('utf-8')).decode('utf-8')
    return f"https://ffxivteamcraft.com/import/{encoded}"

# ------------------------------------------
# GEARSET DATABASE
# ------------------------------------------
GEARSET_DATABASE = {
    "Courtly Lover's Fending (WAR, PLD, GNB, DRK)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**16x** Turali Pigment\n**16x** Mastodon Pelt\n**16x** Double Duracoat\n**10x** Everkeep Resin",
        "exact_items": [
            (1, "Courtly Lover's Hairpin of Fending"), (1, "Courtly Lover's Surcoat of Fending"),
            (1, "Courtly Lover's Gauntlets of Fending"), (1, "Courtly Lover's Breeches of Fending"),
            (1, "Courtly Lover's Boots of Fending"), (1, "Courtly Lover's Earrings of Fending"),
            (1, "Courtly Lover's Choker of Fending"), (1, "Courtly Lover's Wristlet of Fending"),
            (2, "Courtly Lover's Ring of Fending")
        ]
    },
    "Courtly Lover's Maiming (DRG, RPR)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**16x** Turali Pigment\n**16x** Mastodon Pelt\n**16x** Double Duracoat\n**10x** Everkeep Resin",
        "exact_items": [
            (1, "Courtly Lover's Hairpin of Maiming"), (1, "Courtly Lover's Surcoat of Maiming"),
            (1, "Courtly Lover's Gauntlets of Maiming"), (1, "Courtly Lover's Breeches of Maiming"),
            (1, "Courtly Lover's Boots of Maiming"), (1, "Courtly Lover's Earrings of Slaying"),
            (1, "Courtly Lover's Choker of Slaying"), (1, "Courtly Lover's Wristlet of Slaying"),
            (2, "Courtly Lover's Ring of Slaying")
        ]
    },
    "Courtly Lover's Striking (MNK, SAM)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**20x** Turali Pigment\n**16x** Mastodon Pelt\n**12x** Everkeep Resin\n**8x** Double Duracoat\n**2x** Insulating Varnish",
        "exact_items": [
            (1, "Courtly Lover's Temple Chain of Striking"), (1, "Courtly Lover's Cloak of Striking"),
            (1, "Courtly Lover's Armguards of Striking"), (1, "Courtly Lover's Brais of Striking"),
            (1, "Courtly Lover's Boots of Striking"), (1, "Courtly Lover's Earrings of Slaying"),
            (1, "Courtly Lover's Choker of Slaying"), (1, "Courtly Lover's Wristlet of Slaying"),
            (2, "Courtly Lover's Ring of Slaying")
        ]
    },
    "Courtly Lover's Aiming (DNC, BRD, MCH)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**18x** Turali Pigment\n**16x** Everkeep Resin\n**14x** Mastodon Pelt\n**8x** Double Duracoat\n**2x** Insulating Varnish",
        "exact_items": [
            (1, "Courtly Lover's Hairpin of Aiming"), (1, "Courtly Lover's Shirt of Aiming"),
            (1, "Courtly Lover's Halfgloves of Aiming"), (1, "Courtly Lover's Trousers of Aiming"),
            (1, "Courtly Lover's Shoes of Aiming"), (1, "Courtly Lover's Earrings of Aiming"),
            (1, "Courtly Lover's Choker of Aiming"), (1, "Courtly Lover's Wristlet of Aiming"),
            (2, "Courtly Lover's Ring of Aiming")
        ]
    },
    "Courtly Lover's Scouting (NIN, VPR)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**18x** Turali Pigment\n**16x** Everkeep Resin\n**14x** Mastodon Pelt\n**8x** Double Duracoat\n**2x** Insulating Varnish",
        "exact_items": [
            (1, "Courtly Lover's Hairpin of Scouting"), (1, "Courtly Lover's Shirt of Scouting"),
            (1, "Courtly Lover's Halfgloves of Scouting"), (1, "Courtly Lover's Trousers of Scouting"),
            (1, "Courtly Lover's Shoes of Scouting"), (1, "Courtly Lover's Earrings of Aiming"),
            (1, "Courtly Lover's Choker of Aiming"), (1, "Courtly Lover's Wristlet of Aiming"),
            (2, "Courtly Lover's Ring of Aiming")
        ]
    },
    "Courtly Lover's Healing (WHM, AST, SCH, SGE)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**20x** Turali Pigment\n**16x** Mastodon Pelt\n**10x** Everkeep Resin\n**10x** Double Duracoat\n**2x** Insulating Varnish",
        "exact_items": [
            (1, "Courtly Lover's Hood of Healing"), (1, "Courtly Lover's Longcoat of Healing"),
            (1, "Courtly Lover's Gloves of Healing"), (1, "Courtly Lover's Pantaloons of Healing"),
            (1, "Courtly Lover's Shoes of Healing"), (1, "Courtly Lover's Earrings of Healing"),
            (1, "Courtly Lover's Choker of Healing"), (1, "Courtly Lover's Wristlet of Healing"),
            (2, "Courtly Lover's Ring of Healing")
        ]
    },
    "Courtly Lover's Casting (BLM, SMN, RDM, PCT)": {
        "cost_type": "Mathematics Tomestones",
        "cost_amount": 1160,
        "mats": "**20x** Turali Pigment\n**16x** Mastodon Pelt\n**10x** Everkeep Resin\n**10x** Double Duracoat\n**2x** Insulating Varnish",
        "exact_items": [
            (1, "Courtly Lover's Hood of Casting"), (1, "Courtly Lover's Longcoat of Casting"),
            (1, "Courtly Lover's Gloves of Casting"), (1, "Courtly Lover's Pantaloons of Casting"),
            (1, "Courtly Lover's Shoes of Casting"), (1, "Courtly Lover's Earrings of Casting"),
            (1, "Courtly Lover's Choker of Casting"), (1, "Courtly Lover's Wristlet of Casting"),
            (2, "Courtly Lover's Ring of Casting")
        ]
    },
    "Crested Crafting (All Crafters)": {
        "cost_type": "Variable Scrips",
        "cost_amount": "N/A",
        "mats": "**12x** Shaaloani Coke\n**14x** Neo Abrasive\n**27x** Mason's Abrasive\n**20x** Hydrophobic Preservative\n**32x** Diatryma Pelt\n**16x** Cronopio Skin\n**27x** Condensed Solution",
        "exact_items": [
            (1, "Crested Cap of Crafting"), (1, "Crested Coat of Crafting"), (1, "Crested Gloves of Crafting"),
            (1, "Crested Hose of Crafting"), (1, "Crested Shoes of Crafting"), (1, "Crested Earrings of Crafting"),
            (1, "Crested Necklace of Crafting"), (1, "Crested Wristband of Crafting"), (2, "Crested Ring of Crafting")
        ]
    },
    "Crested Gathering (All Gatherers)": {
        "cost_type": "Variable Scrips",
        "cost_amount": "N/A",
        "mats": "**16x** Shaaloani Coke\n**8x** Neo Abrasive\n**27x** Mason's Abrasive\n**20x** Hydrophobic Preservative\n**30x** Diatryma Pelt\n**22x** Cronopio Skin\n**27x** Condensed Solution",
        "exact_items": [
            (1, "Crested Cap of Gathering"), (1, "Crested Coat of Gathering"), (1, "Crested Halfgloves of Gathering"),
            (1, "Crested Bottoms of Gathering"), (1, "Crested Boots of Gathering"), (1, "Crested Earrings of Gathering"),
            (1, "Crested Necklace of Gathering"), (1, "Crested Wristband of Gathering"), (2, "Crested Ring of Gathering")
        ]
    }
}

# ==========================================
# BOT INITIALIZATION (WITH DUMMY SERVER FOR RENDER)
# ==========================================
class CraftingBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        # Create a tiny background web server to satisfy Render's port requirement
        app = aiohttp.web.Application()
        app.router.add_get('/', lambda request: aiohttp.web.Response(text="Bot is alive!"))
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        
        # Render automatically provides a PORT environment variable
        port = int(os.environ.get("PORT", 8080))
        site = aiohttp.web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"🌐 Dummy web server listening on port {port} to keep Render happy!")

bot = CraftingBot()

# ==========================================
# MODALS & VIEWS
# ==========================================
class GearsetModal(discord.ui.Modal):
    def __init__(self, selected_set: str):
        super().__init__(title=f"Order: {selected_set}"[:45])
        self.selected_set = selected_set

    weapon_input = discord.ui.TextInput(label="Job Weapon/Tools (Optional)", placeholder="e.g. WAR, PLD, GSM (Leave blank for armor only)", required=False)
    notes_input = discord.ui.TextInput(label="Exceptions / Notes", style=discord.TextStyle.paragraph, placeholder="e.g. Accessories only, skip the chestpiece, etc.", required=False)
    recipient = discord.ui.TextInput(label="Who is this for?", placeholder="Character Name", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        selected_set = self.selected_set
        weapon_req = self.weapon_input.value.strip()
        teamcraft_payload = []
        tc_url = None
        
        if selected_set in GEARSET_DATABASE:
            set_data = GEARSET_DATABASE[selected_set]
            total_cost = set_data["cost_amount"]
            combined_mats = [f"**Required Materials:**\n{set_data['mats']}"]
            cost_type = set_data["cost_type"]
            
            if "exact_items" in set_data:
                async with aiohttp.ClientSession(headers=HEADERS) as session:
                    for qty, item_name in set_data["exact_items"]:
                        params = {"sheets": "Item", "query": f'Name~"{item_name}"', "limit": "1"}
                        try:
                            async with session.get(f"{XIVAPI_BASE}/search", params=params, timeout=5) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    results = data.get('results', [])
                                    if results:
                                        teamcraft_payload.append((results[0].get('row_id'), qty))
                        except Exception:
                            pass 
            if teamcraft_payload:
                tc_url = generate_teamcraft_url(teamcraft_payload)
        else:
            set_data = {"cost_amount": 0, "mats": "*(Mats not found)*"}
            total_cost = 0
            cost_type = "Unknown Currency"
            combined_mats = ["**Required Materials:**\n*(Missing from database)*"]
            
        items_summary_str = f"**1x Full {self.selected_set}**"
        
        if weapon_req:
            items_summary_str += f"\n**Weapon/Tool:** {weapon_req}"
            if isinstance(total_cost, int):
                total_cost += 500
            combined_mats.append(f"**Weapon/Tool:**\n*(Weapon mats must be calculated manually)*")

        if self.notes_input.value.strip():
            items_summary_str += f"\n\n*📝 Notes: {self.notes_input.value.strip()}*"

        guild = interaction.guild
        active_orders_channel = discord.utils.get(guild.text_channels, name="open-orders")
        if not active_orders_channel:
            active_orders_channel = await guild.create_text_channel("open-orders")
        
        embed = discord.Embed(title="🆕 Full Gearset Order Received", color=discord.Color.purple())
        embed.add_field(name="Recipient Target", value=self.recipient.value, inline=False)
        embed.add_field(name="Requested Order", value=items_summary_str, inline=False)
        embed.add_field(name="Requested By", value=interaction.user.mention, inline=False)
        
        if tc_url:
            embed.add_field(name="Teamcraft Link", value=f"[🛠️ Open Recipe List]({tc_url})", inline=False)
            
        embed.add_field(name=f"💎 Total Base Cost: {total_cost} {cost_type}", value="\n\n".join(combined_mats), inline=False)

        root_msg = await active_orders_channel.send(embed=embed)
        thread = await root_msg.create_thread(name=f"Order - {self.recipient.value[:20]}")
        
        control_msg = await thread.send("Use the dashboard below to coordinate this craft.", view=OrderControlView())

        crafter_role = discord.utils.get(guild.roles, name=CRAFTER_ROLE_NAME)
        role_ping = f"<@&{crafter_role.id}>" if crafter_role else ""
        ping_msg = await thread.send(f"{interaction.user.mention} {role_ping}")
        await ping_msg.delete()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO orders (control_message_id, root_message_id, requester_id, items_summary, recipient, tc_url) VALUES (?, ?, ?, ?, ?, ?)",
                       (control_msg.id, root_msg.id, interaction.user.id, items_summary_str, self.recipient.value, tc_url))
        conn.commit()
        conn.close()

        await interaction.followup.send("✅ Order perfectly routed to `#open-orders` with Teamcraft link attached!", ephemeral=True)


class OrderModal(discord.ui.Modal, title="Bulk Crafting Request"):
    items_input = discord.ui.TextInput(label="Items Needed (Qty Item Name OR TC Link)", style=discord.TextStyle.paragraph, placeholder="Example:\n1x Courtly Lovers Brush\nOR just paste a Teamcraft link!", required=True)
    recipient = discord.ui.TextInput(label="Who is this for?", placeholder="Character Name", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lines = self.items_input.value.strip().split('\n')
        final_items_list = []
        teamcraft_payload = []
        provided_tc_url = None

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            for line in lines:
                line = line.strip()
                if not line: continue
                
                tc_match = re.search(r'(https?://[a-zA-Z0-9.\-]*ffxivteamcraft\.com\S*)', line, re.IGNORECASE)
                if tc_match:
                    provided_tc_url = tc_match.group(1)
                    continue 

                match = re.match(r'^(\d+)\s*[xX]?\s*(.*)$', line)
                if match:
                    qty, raw_item_name = int(match.group(1)), match.group(2).strip()
                else:
                    qty, raw_item_name = 1, line.strip()

                try:
                    search_url = f"{XIVAPI_BASE}/search"
                    safe_raw = raw_item_name.replace('"', '')
                    
                    # Pass 1: Exact Substring
                    params_exact = {"sheets": "Item", "query": f'Name~"{safe_raw}"', "limit": "1"}
                    
                    # Pass 2: Fuzzy (Stripped punctuation)
                    clean_search = re.sub(r"[^\w\s]", "", safe_raw)
                    params_fuzzy = {"sheets": "Item", "query": clean_search, "limit": "1"}
                    
                    # Pass 3: Wildcard Match
                    wildcard_query = " ".join([f"*{word}*" for word in clean_search.split()])
                    params_wildcard = {"sheets": "Item", "query": wildcard_query, "limit": "1"}
                    
                    # Execute Search Flow
                    async with session.get(search_url, params=params_exact, timeout=3) as resp:
                        data = await resp.json() if resp.status == 200 else {}
                        results = data.get('results', [])
                        
                    if not results and clean_search != safe_raw:
                        async with session.get(search_url, params=params_fuzzy, timeout=3) as resp:
                            data = await resp.json() if resp.status == 200 else {}
                            results = data.get('results', [])
                            
                    if not results:
                        async with session.get(search_url, params=params_wildcard, timeout=3) as resp:
                            data = await resp.json() if resp.status == 200 else {}
                            results = data.get('results', [])

                    if results:
                        item_id = results[0].get('row_id')
                        official_name = results[0].get('fields', {}).get('Name') or raw_item_name
                        final_items_list.append(f"• **{qty}x** {official_name}")
                        teamcraft_payload.append((item_id, qty))
                    else:
                        final_items_list.append(f"• **{qty}x** {raw_item_name} *(⚠️ Unverified)*")
                except Exception as e:
                    print(f"Bulk Search Error: {e}")
                    final_items_list.append(f"• **{qty}x** {raw_item_name} *(⚠️ Catalog Offline)*")

        if not final_items_list and not provided_tc_url:
            await interaction.followup.send("❌ No valid text or URL found.", ephemeral=True)
            return

        items_summary_str = "\n".join(final_items_list) if final_items_list else "*(Items contained within provided Teamcraft URL)*"
        final_tc_url = provided_tc_url or (generate_teamcraft_url(teamcraft_payload) if teamcraft_payload else None)
        
        guild = interaction.guild
        active_orders_channel = discord.utils.get(guild.text_channels, name="open-orders")
        if not active_orders_channel:
            active_orders_channel = await guild.create_text_channel("open-orders")
        
        embed = discord.Embed(title="🆕 Crafting Order Received", color=discord.Color.gold())
        embed.add_field(name="Recipient Target", value=self.recipient.value, inline=False)
        embed.add_field(name="Requested Items", value=items_summary_str, inline=False)
        embed.add_field(name="Requested By", value=interaction.user.mention, inline=True)
        if final_tc_url:
            embed.add_field(name="Teamcraft Link", value=f"[🛠️ Open Recipe List]({final_tc_url})", inline=False)

        root_msg = await active_orders_channel.send(embed=embed)
        thread = await root_msg.create_thread(name=f"Order - {self.recipient.value[:20]}")
        control_msg = await thread.send("Use the dashboard below to coordinate this craft.", view=OrderControlView())

        crafter_role = discord.utils.get(guild.roles, name=CRAFTER_ROLE_NAME)
        role_ping = f"<@&{crafter_role.id}>" if crafter_role else ""
        ping_msg = await thread.send(f"{interaction.user.mention} {role_ping}")
        await ping_msg.delete()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO orders (control_message_id, root_message_id, requester_id, items_summary, recipient, tc_url) VALUES (?, ?, ?, ?, ?, ?)",
                       (control_msg.id, root_msg.id, interaction.user.id, items_summary_str, self.recipient.value, final_tc_url))
        conn.commit()
        conn.close()

        await interaction.followup.send("✅ Bulk order submitted successfully!", ephemeral=True)


class RequestOrderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="🛡️ Select a Full Gearset to Order...",
        custom_id="gearset_select",
        row=0,
        options=[
            discord.SelectOption(label="Courtly Lover's Fending (WAR, PLD, GNB, DRK)"),
            discord.SelectOption(label="Courtly Lover's Maiming (DRG, RPR)"),
            discord.SelectOption(label="Courtly Lover's Striking (MNK, SAM)"),
            discord.SelectOption(label="Courtly Lover's Scouting (NIN, VPR)"),
            discord.SelectOption(label="Courtly Lover's Aiming (DNC, BRD, MCH)"),
            discord.SelectOption(label="Courtly Lover's Casting (BLM, SMN, RDM, PCT)"),
            discord.SelectOption(label="Courtly Lover's Healing (WHM, AST, SCH, SGE)"),
            discord.SelectOption(label="Crested Crafting (All Crafters)"),
            discord.SelectOption(label="Crested Gathering (All Gatherers)")
        ]
    )
    async def gearset_dropdown(self, interaction: discord.Interaction, select: discord.ui.Select):
        selected_set = select.values[0]
        await interaction.response.send_modal(GearsetModal(selected_set))

    @discord.ui.button(label="Bulk Paste Order ➕", style=discord.ButtonStyle.primary, custom_id="open_order_modal", row=1)
    async def place_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderModal())


class OrderControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Order 🛠️", style=discord.ButtonStyle.success, custom_id="claim_order")
    async def claim_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT root_message_id FROM orders WHERE control_message_id=?", (interaction.message.id,))
        row = cursor.fetchone()
        
        if row:
            root_msg_id = row[0]
            cursor.execute("UPDATE orders SET crafter_id=?, status='Claimed' WHERE control_message_id=?", (interaction.user.id, interaction.message.id))
            conn.commit()
            
            channel = interaction.channel.parent
            root_msg = await channel.fetch_message(root_msg_id)
            embed = root_msg.embeds[0]
            embed.color = discord.Color.blue()
            embed.title = "🛠️ Order Claimed"
            
            crafter_field_exists = False
            for i, field in enumerate(embed.fields):
                if field.name == "Claimed By":
                    embed.set_field_at(i, name="Claimed By", value=interaction.user.mention, inline=True)
                    crafter_field_exists = True
                    break
            if not crafter_field_exists:
                embed.add_field(name="Claimed By", value=interaction.user.mention, inline=True)
                
            await root_msg.edit(embed=embed)
            await interaction.response.send_message(f"Order claimed by {interaction.user.mention}!", ephemeral=False)
        else:
            await interaction.response.send_message("Database error: Could not find order.", ephemeral=True)
        conn.close()

    @discord.ui.button(label="Mats Provided 📦", style=discord.ButtonStyle.secondary, custom_id="mats_provided")
    async def mats_provided(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT root_message_id FROM orders WHERE control_message_id=?", (interaction.message.id,))
        row = cursor.fetchone()
        
        if row:
            root_msg_id = row[0]
            channel = interaction.channel.parent
            root_msg = await channel.fetch_message(root_msg_id)
            embed = root_msg.embeds[0]
            
            mats_field_exists = False
            for i, field in enumerate(embed.fields):
                if field.name == "Materials Status":
                    embed.set_field_at(i, name="Materials Status", value="✅ **Provided by Buyer**", inline=False)
                    mats_field_exists = True
                    break
            if not mats_field_exists:
                embed.add_field(name="Materials Status", value="✅ **Provided by Buyer**", inline=False)
                
            await root_msg.edit(embed=embed)
            button.disabled = True
            await interaction.message.edit(view=self)
            await interaction.response.send_message("Materials have been marked as provided!", ephemeral=False)
        else:
            await interaction.response.send_message("Database error: Could not find order.", ephemeral=True)
        conn.close()

    @discord.ui.button(label="Complete ✅", style=discord.ButtonStyle.primary, custom_id="complete_order")
    async def complete_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_order_closure(interaction, "Completed", discord.Color.green())

    @discord.ui.button(label="Cancel ❌", style=discord.ButtonStyle.danger, custom_id="cancel_order")
    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_order_closure(interaction, "Canceled", discord.Color.red())

    async def process_order_closure(self, interaction: discord.Interaction, status: str, color: discord.Color):
        await interaction.response.send_message(f"Order marked as {status}. Deleting thread and moving log...", ephemeral=True)
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT root_message_id, requester_id, recipient FROM orders WHERE control_message_id=?", (interaction.message.id,))
        row = cursor.fetchone()
        
        if row:
            root_msg_id, requester_id, recipient = row
            cursor.execute("UPDATE orders SET status=? WHERE control_message_id=?", (status, interaction.message.id))
            conn.commit()
            
            guild = interaction.guild
            closed_channel = discord.utils.get(guild.text_channels, name="closed-orders")
            if not closed_channel:
                closed_channel = await guild.create_text_channel("closed-orders")
                
            # Safely grab the parent channel before we delete the thread
            open_channel = interaction.channel.parent
            current_thread = interaction.channel
            
            try:
                root_msg = await open_channel.fetch_message(root_msg_id)
                embed = root_msg.embeds[0]
                embed.color = color
                embed.title = f"📦 Order {status}"
                
                embed.set_footer(text=f"Order ID: {interaction.message.id}")
                
                await closed_channel.send(
                    content=f"Crafting ticket for <@{requester_id}> was **{status.lower()}** by {interaction.user.mention}.", 
                    embed=embed,
                    view=ClosedOrderView()
                )
                
                if status == "Completed":
                    try:
                        requester = await guild.fetch_member(requester_id)
                        await requester.send(f"✅ Your crafting order for **{recipient}** has been completed by {interaction.user.name}!")
                    except: pass
                
                # Safely delete the thread, then delete the root message
                await current_thread.delete()
                await root_msg.delete()

            except Exception as e:
                print(f"Error moving thread: {e}")
        conn.close()


class ClosedOrderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Restore Order ♻️", style=discord.ButtonStyle.secondary, custom_id="restore_order")
    async def restore_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        footer_text = embed.footer.text or ""
        
        if not footer_text.startswith("Order ID: "):
            await interaction.response.send_message("Cannot restore this order (missing ID).", ephemeral=True)
            return
            
        old_control_id = int(footer_text.replace("Order ID: ", ""))
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT requester_id, items_summary, recipient, tc_url FROM orders WHERE control_message_id=?", (old_control_id,))
        row = cursor.fetchone()
        
        if not row:
            await interaction.response.send_message("Database record not found. Cannot restore.", ephemeral=True)
            conn.close()
            return
            
        requester_id, items_summary_str, recipient_val, tc_url = row
        
        embed.color = discord.Color.gold()
        embed.title = "🆕 Restored Crafting Order"
        embed.set_footer(text=None) 
        
        guild = interaction.guild
        open_channel = discord.utils.get(guild.text_channels, name="open-orders")
        if not open_channel:
            open_channel = await guild.create_text_channel("open-orders")
            
        root_msg = await open_channel.send(embed=embed)
        thread = await root_msg.create_thread(name=f"Order - {recipient_val[:20]}")
        control_msg = await thread.send("Use the dashboard below to coordinate this craft.", view=OrderControlView())
        
        crafter_role = discord.utils.get(guild.roles, name=CRAFTER_ROLE_NAME)
        role_ping = f"<@&{crafter_role.id}>" if crafter_role else ""
        ping_msg = await thread.send(f"Order restored from archives! {role_ping}")
        await ping_msg.delete()
        
        cursor.execute("UPDATE orders SET control_message_id=?, root_message_id=?, status='Pending' WHERE control_message_id=?", (control_msg.id, root_msg.id, old_control_id))
        conn.commit()
        conn.close()
        
        await interaction.message.delete()
        await interaction.response.send_message("Order restored to `#open-orders`!", ephemeral=True)


# ==========================================
# COMMANDS & STARTUP
# ==========================================
@bot.event
async def on_ready():
    init_db()
    bot.add_view(RequestOrderView())
    bot.add_view(OrderControlView())
    bot.add_view(ClosedOrderView())
    
    # Automatically rebuild the database from Discord if the DB was wiped!
    await rebuild_database()
    
    print(f"Operational running profile authenticated as {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash configurations.")
    except Exception as e:
        print(f"Sync issue found: {e}")

@bot.command(name="ordersetup")
@commands.has_permissions(administrator=True)
async def ordersetup(ctx):
    guild = ctx.guild
    orders_channel = discord.utils.get(guild.text_channels, name="place-order")
    
    if not orders_channel:
        orders_channel = await guild.create_text_channel("place-order")

    embed = discord.Embed(
        title="🏛️ Crafting Logistics Depot",
        description="Need items manufactured? You can order in three ways:\n\n**Option 1: Full Gearset**\nUse the `Select a Full Gearset` dropdown menu below to quickly request a full tier set with auto-calculated materials.\n\n**Option 2: The Bulk Board**\nClick `Bulk Paste Order` to paste a list of custom items or a Teamcraft link.\n\n**Option 3: The /order Command**\nType `/order` anywhere to quickly lookup a specific item using autocomplete!",
        color=discord.Color.purple()
    )
    await orders_channel.send(embed=embed, view=RequestOrderView())
    await ctx.send(f"Dashboard frame successfully placed down inside <#{orders_channel.id}>!")


# --- V2 AUTOCOMPLETE COMMAND ---
@bot.tree.command(name="order", description="Order a specific item with autocomplete.")
@app_commands.describe(item_name="Search for the item you need to craft", quantity="How many do you need?")
async def order_single(interaction: discord.Interaction, item_name: str, quantity: int = 1):
    await interaction.response.defer(ephemeral=True)
    
    # Automatically set the recipient to the person who ran the command!
    recipient = interaction.user.display_name
    
    item_id = None
    if "|" in item_name:
        parts = item_name.split("|")
        item_id = parts[0]
        official_name = parts[1]
    else:
        official_name = item_name
        # Fallback Background Lookup: If they didn't click the autocomplete result
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            try:
                safe_name = official_name.replace('"', '')
                params = {"sheets": "Item", "query": f'Name~"{safe_name}"', "limit": "1"}
                async with session.get(f"{XIVAPI_BASE}/search", params=params, timeout=3) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get('results', [])
                        if results:
                            item_id = results[0].get('row_id')
                            official_name = results[0].get('fields', {}).get('Name') or official_name
            except Exception:
                pass
        
    items_summary_str = f"• **{quantity}x** {official_name}"
    
    tc_url = None
    if item_id is not None:
        tc_url = generate_teamcraft_url([(item_id, quantity)])
        
    guild = interaction.guild
    active_orders_channel = discord.utils.get(guild.text_channels, name="open-orders")
    if not active_orders_channel:
        active_orders_channel = await guild.create_text_channel("open-orders")
    
    embed = discord.Embed(title="🆕 Single Item Order", color=discord.Color.blue())
    embed.add_field(name="Recipient Target", value=recipient, inline=False)
    embed.add_field(name="Requested Item", value=items_summary_str, inline=False)
    embed.add_field(name="Requested By", value=interaction.user.mention, inline=True)
    if tc_url:
        embed.add_field(name="Teamcraft Link", value=f"[🛠️ Open Recipe List]({tc_url})", inline=False)

    root_msg = await active_orders_channel.send(embed=embed)
    thread = await root_msg.create_thread(name=f"Order - {recipient[:20]}")
    control_msg = await thread.send("Use the dashboard below to coordinate this craft.", view=OrderControlView())

    crafter_role = discord.utils.get(guild.roles, name=CRAFTER_ROLE_NAME)
    role_ping = f"<@&{crafter_role.id}>" if crafter_role else ""
    ping_msg = await thread.send(f"{interaction.user.mention} {role_ping}")
    await ping_msg.delete()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO orders (control_message_id, root_message_id, requester_id, items_summary, recipient, tc_url) VALUES (?, ?, ?, ?, ?, ?)",
                   (control_msg.id, root_msg.id, interaction.user.id, items_summary_str, recipient, tc_url))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"✅ Your order for {quantity}x {official_name} has been routed to `#open-orders`!", ephemeral=True)

@order_single.autocomplete("item_name")
async def item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not current or len(current) < 3:
        return []
    
    search_url = f"{XIVAPI_BASE}/search"
    safe_current = current.replace('"', '')
    
    # Pass 1: Substring Match (CRITICAL for partial typing like "Boiled E")
    params_exact = {"sheets": "Item", "query": f'Name~"{safe_current}"', "limit": "15"}
    
    # Pass 2: Stripped Fuzzy Match (Catches missing apostrophes like "courtly lovers")
    clean_search = re.sub(r"[^\w\s]", "", safe_current)
    params_fuzzy = {"sheets": "Item", "query": clean_search, "limit": "15"}
    
    # Pass 3: Wildcard Match (Catches missing middle words like "courtly brush")
    wildcard_query = " ".join([f"*{word}*" for word in clean_search.split()])
    params_wildcard = {"sheets": "Item", "query": wildcard_query, "limit": "15"}
    
    choices = {}
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        try:
            # Run Pass 1
            async with session.get(search_url, params=params_exact, timeout=2) as resp:
                data = await resp.json() if resp.status == 200 else {}
                results = data.get('results', [])
            
            # Run Pass 2 if needed
            if not results and clean_search != safe_current:
                async with session.get(search_url, params=params_fuzzy, timeout=2) as resp:
                    data = await resp.json() if resp.status == 200 else {}
                    results = data.get('results', [])
            
            # Run Pass 3 if still empty
            if not results:
                async with session.get(search_url, params=params_wildcard, timeout=2) as resp:
                    data = await resp.json() if resp.status == 200 else {}
                    results = data.get('results', [])
            
            # Build the dropdown menu
            for item in results:
                official_name = item['fields'].get('Name', 'Unknown')
                item_id = item.get('row_id') or item.get('id')
                val_str = f"{item_id}|{official_name}"[:100]
                
                # Prevent duplicates in the dropdown
                if official_name not in choices:
                    choices[official_name] = app_commands.Choice(name=official_name[:100], value=val_str)
                    
        except Exception as e:
            print(f"[Autocomplete Error] {e}")
            
    return list(choices.values())[:25]

bot.run(TOKEN)
