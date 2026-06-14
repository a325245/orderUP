import os
import json
import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web
import base64
import re
import sqlite3
import traceback
from datetime import timedelta
from dotenv import load_dotenv

# Load Environment variables
load_dotenv()

# Fallback Configuration Token Slot
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN or TOKEN == "your_bot_token_here":
    TOKEN = "YOUR_TOKEN_HERE"

CRAFTER_ROLE_NAME = "Hearthkeepers"
XIVAPI_BASE = "https://v2.xivapi.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# --- PERMANENT STORAGE ROUTING ---
RAILWAY_VOLUME_PATH = "/app/data"

if os.path.exists(RAILWAY_VOLUME_PATH):
    BASE_DIR = RAILWAY_VOLUME_PATH
    print("💾 Connected to permanent Railway Volume!")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    print("💻 Running locally. Using local file storage.")

DB_PATH = os.path.join(BASE_DIR, "orders.db")
JSON_PATH = os.path.join(BASE_DIR, "gear_database.json")

# GLOBAL SHOPPING CART MEMORY
USER_CARTS = {}
# ---------------------------------

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
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN reminded INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()

async def rebuild_database(bot):
    print("🔄 Scanning Discord to rebuild the database...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for guild in bot.guilds:
        # Rebuild Open Orders
        open_channel = discord.utils.get(guild.text_channels, name="open-orders")
        if open_channel:
            for thread in open_channel.threads:
                try:
                    control_msg_id = None
                    async for msg in thread.history(limit=5, oldest_first=True):
                        if msg.author == bot.user and msg.components:
                            control_msg_id = msg.id
                            break
                    if not control_msg_id: continue
                    
                    cursor.execute("SELECT 1 FROM orders WHERE control_message_id=?", (control_msg_id,))
                    if cursor.fetchone(): continue
                    
                    root_msg = await open_channel.fetch_message(thread.id)
                    if not root_msg.embeds: continue
                    embed = root_msg.embeds[0]
                    
                    recipient, items_summary, tc_url = "", "", None
                    requester_id, crafter_id = 0, None
                    status = 'Claimed' if "Claimed" in embed.title else 'Pending'
                    
                    for field in embed.fields:
                        if field.name == "Recipient Target": recipient = field.value
                        elif field.name in ["Requested Items", "Requested Item", "Armor Targets", "📦 Required Materials"]: items_summary = field.value
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
                except Exception:
                    pass

        # Rebuild Closed Orders
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
                            elif field.name in ["Requested Items", "Requested Item", "Armor Targets", "📦 Required Materials"]: items_summary = field.value
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

def load_gear_data():
    try:
        with open(JSON_PATH, "r", encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️ WARNING: {JSON_PATH} not found!")
        return {"gearsets": {}, "job_mappings": {}, "weapons": {}}

GEAR_DB = load_gear_data()

def generate_teamcraft_url(teamcraft_payload):
    import_str = ";".join([f"{item_id},null,{qty}" for item_id, qty in teamcraft_payload])
    encoded = base64.b64encode(import_str.encode('utf-8')).decode('utf-8')
    return f"https://ffxivteamcraft.com/import/{encoded}"

def validate_database_schema(data):
    if not isinstance(data, dict): return "The root of the file must be a JSON object {}."
    required_keys = ["gearsets", "job_mappings", "weapons"]
    missing = [k for k in required_keys if k not in data]
    if missing: return f"Missing primary categories: {', '.join(missing)}"
    if not isinstance(data["gearsets"], dict): return "'gearsets' must be an object {} containing your tier lists."
        
    for gs_name, gs_data in data["gearsets"].items():
        if "currency" not in gs_data: return f"Gearset '{gs_name}' is missing the 'currency' label."
        if "materials_per_piece" not in gs_data: return f"Gearset '{gs_name}' is missing 'materials_per_piece'."
        if not isinstance(gs_data["materials_per_piece"], dict): return f"'{gs_name}' -> 'materials_per_piece' must be an object {{}}."
        for piece, p_data in gs_data["materials_per_piece"].items():
            if not isinstance(p_data, dict) or "mats" not in p_data or "name" not in p_data:
                return f"Piece '{piece}' in '{gs_name}' must contain explicit 'name' and 'mats'."
            
    if not isinstance(data["weapons"], dict): return "'weapons' must be an object {} mapping jobs to weapon data."
    for job, w_data in data["weapons"].items():
        if "has_offhand" not in w_data or not isinstance(w_data["has_offhand"], bool):
            return f"Job '{job}' must have a true/false 'has_offhand' value."
        if "mats_mh" not in w_data or "name_mh" not in w_data: 
            return f"Job '{job}' is missing main hand data ('mats_mh' or 'name_mh')."
    return None

# ==========================================
# BOT INITIALIZATION
# ==========================================
class CraftingBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        app = web.Application()
        app.router.add_get('/', lambda request: web.Response(text="Bot is alive!"))
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"🌐 Web server listening on port {port}")
        self.check_stale_orders.start()

    @tasks.loop(hours=12)
    async def check_stale_orders(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT control_message_id, root_message_id, recipient FROM orders WHERE status IN ('Pending', 'Claimed') AND reminded = 0")
        rows = cursor.fetchall()
        now = discord.utils.utcnow()
        for row in rows:
            control_msg_id, root_msg_id, recipient = row
            created_at = discord.utils.snowflake_time(control_msg_id)
            if now - created_at > timedelta(days=3):
                for guild in self.guilds:
                    try:
                        thread = guild.get_thread(root_msg_id)
                        if thread:
                            crafter_role = discord.utils.get(guild.roles, name=CRAFTER_ROLE_NAME)
                            role_ping = f"<@&{crafter_role.id}>" if crafter_role else "Crafters"
                            await thread.send(f"⚠️ **Timeout Reminder:** {role_ping}, this order for **{recipient}** has been open for over 3 days without being completed!")
                            cursor.execute("UPDATE orders SET reminded = 1 WHERE control_message_id = ?", (control_msg_id,))
                            conn.commit()
                            break 
                    except Exception:
                        pass
        conn.close()

    @check_stale_orders.before_loop
    async def before_check(self):
        await self.wait_until_ready()

bot = CraftingBot()

# ==========================================
# DASHBOARD VIEWS
# ==========================================
class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Gearset Wizard 🛠️", style=discord.ButtonStyle.primary, custom_id="open_wizard_btn")
    async def open_wizard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            content="**Step 1:** Select the gearset requested:",
            view=OrderWizardView(),
            ephemeral=True
        )

    @discord.ui.button(label="Items / Gear ➕", style=discord.ButtonStyle.secondary, custom_id="open_bulk_btn")
    async def open_bulk(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderModal())

# ==========================================
# BULK FREESTYLE ORDER MODAL
# ==========================================
class OrderModal(discord.ui.Modal, title="Bulk Crafting Request"):
    items_input = discord.ui.TextInput(label="Items Needed (Qty Item Name OR TC Link)", style=discord.TextStyle.paragraph, placeholder="Example:\n1x Courtly Lovers Filbert Brush\nOR just paste a Teamcraft link!", required=True)
    recipient = discord.ui.TextInput(label="Who is this for?", placeholder="Character Name", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ **Calculating materials and assembling your order...** Please wait.", ephemeral=True)

        try:
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
                        
                        params_exact = {"sheets": "Item", "query": f'Name~"{safe_raw}"', "limit": "1"}
                        clean_search = re.sub(r"[^\w\s]", "", safe_raw)
                        params_fuzzy = {"sheets": "Item", "query": clean_search, "limit": "1"}
                        wildcard_query = " ".join([f"*{word}*" for word in clean_search.split()])
                        params_wildcard = {"sheets": "Item", "query": wildcard_query, "limit": "1"}
                        
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
                    except Exception:
                        final_items_list.append(f"• **{qty}x** {raw_item_name} *(⚠️ Catalog Offline)*")

            if not final_items_list and not provided_tc_url:
                await interaction.edit_original_response(content="❌ **Error:** No valid text or URL found in your submission.")
                return

            items_summary_str = "\n".join(final_items_list) if final_items_list else "*(Items contained within provided Teamcraft URL)*"
            final_tc_url = provided_tc_url or (generate_teamcraft_url(teamcraft_payload) if teamcraft_payload else None)
            
            guild = interaction.guild
            active_orders_channel = discord.utils.get(guild.text_channels, name="open-orders")
            if not active_orders_channel:
                active_orders_channel = await guild.create_text_channel("open-orders")
            
            embed = discord.Embed(title="🆕 Bulk Crafting Order", color=discord.Color.gold())
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

            await interaction.edit_original_response(content=f"✅ **Bulk order successfully sent to** <#{active_orders_channel.id}>! You can now dismiss this message.")
            
        except Exception as e:
            error_trace = traceback.format_exc()
            print(error_trace)
            await interaction.edit_original_response(content=f"❌ **CRITICAL ERROR in OrderModal:**\n```py\n{e}\n```\nTell the admin to check the logs!")

# ==========================================
# DYNAMIC MULTI-STEP WIZARD VIEWS
# ==========================================
class OrderWizardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        options = []
        for name, data in GEAR_DB.get("gearsets", {}).items():
            options.append(discord.SelectOption(label=name, description=f"Uses {data.get('currency', 'Materials')}"))
        if not options:
            options.append(discord.SelectOption(label="Error: No Data Loaded", value="error"))
        self.add_item(GearsetDropdown(options))

class GearsetDropdown(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Select the base gearset tier...", min_values=1, max_values=1, options=options)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "error":
            await interaction.response.edit_message(content="❌ Database failed to load.", view=None)
            return
        selected_set = self.values[0]
        await interaction.response.edit_message(
            content=f"**Set Chosen:** {selected_set}\n\n**Step 2:** Select the specific armor pieces required (Defaults to all):",
            view=ArmorSelectionView(selected_set)
        )

class ArmorSelectionView(discord.ui.View):
    def __init__(self, selected_set):
        super().__init__(timeout=300)
        self.selected_set = selected_set
        set_pieces = list(GEAR_DB["gearsets"][selected_set]["materials_per_piece"].keys())
        self.add_item(ArmorDropdown(set_pieces))
        self.add_item(ConfirmArmorButton(set_pieces))

class ArmorDropdown(discord.ui.Select):
    def __init__(self, pieces):
        options = [discord.SelectOption(label=piece, default=True) for piece in pieces]
        super().__init__(placeholder="Toggle required components...", min_values=1, max_values=len(pieces), options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

class ConfirmArmorButton(discord.ui.Button):
    def __init__(self, default_pieces):
        super().__init__(label="Confirm Armor ➡️", style=discord.ButtonStyle.primary)
        self.default_pieces = default_pieces
    async def callback(self, interaction: discord.Interaction):
        view: ArmorSelectionView = self.view
        chosen_pieces = self.default_pieces
        for item in view.children:
            if isinstance(item, ArmorDropdown) and getattr(item, 'values', None):
                chosen_pieces = item.values
        await interaction.response.edit_message(
            content=f"**Set:** {view.selected_set}\n**Pieces:** {', '.join(chosen_pieces)}\n\n**Step 3:** Select the jobs that require weapons/tools (Optional):",
            view=JobSelectionView(view.selected_set, chosen_pieces)
        )

class JobSelectionView(discord.ui.View):
    def __init__(self, selected_set, chosen_pieces):
        super().__init__(timeout=300)
        self.selected_set = selected_set
        self.chosen_pieces = chosen_pieces
        category = ""
        for key in GEAR_DB["job_mappings"].keys():
            if key in selected_set:
                category = key
                break
        valid_jobs = GEAR_DB["job_mappings"].get(category, [])
        if valid_jobs:
            self.add_item(JobDropdown(valid_jobs))
        if category in ["Crafting", "Gathering"] or "PLD" in valid_jobs:
            self.add_item(OffhandDropdown())
        self.add_item(ContinueButton())

class JobDropdown(discord.ui.Select):
    def __init__(self, jobs):
        options = []
        for job in jobs:
            w_data = GEAR_DB["weapons"].get(job, {})
            job_name = w_data.get("name_mh", job) if w_data else job
            options.append(discord.SelectOption(label=job, description=job_name[:100]))
        super().__init__(placeholder="Select associated weapons/tools...", min_values=0, max_values=len(options), options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer() 

class OffhandDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Main Hand + Offhand", value="both", default=True),
            discord.SelectOption(label="Main Hand Only", value="mh"),
            discord.SelectOption(label="Offhand Only", value="oh")
        ]
        super().__init__(placeholder="Configure Weapon/Tool Scope...", min_values=1, max_values=1, options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

class ContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Continue to Finalize ➡️", style=discord.ButtonStyle.success)
    async def callback(self, interaction: discord.Interaction):
        view: JobSelectionView = self.view
        selected_jobs = []
        tool_scope = "both"
        for item in view.children:
            if isinstance(item, JobDropdown) and item.values:
                selected_jobs = item.values
            elif isinstance(item, OffhandDropdown) and item.values:
                tool_scope = item.values[0]
        await interaction.response.send_modal(
            FinalizeOrderModal(view.selected_set, view.chosen_pieces, selected_jobs, tool_scope)
        )

# ==========================================
# FINALIZATION MODAL
# ==========================================
class FinalizeOrderModal(discord.ui.Modal):
    def __init__(self, gearset, pieces, jobs, tool_scope):
        super().__init__(title="Finalize Order")
        self.gearset = gearset
        self.pieces = pieces
        self.jobs = jobs
        self.tool_scope = tool_scope

        self.recipient = discord.ui.TextInput(label="Recipient Character Name", placeholder="Who gets this gear?", required=True)
        self.notes = discord.ui.TextInput(label="Special Requests / Exceptions", style=discord.TextStyle.paragraph, required=False)
        self.add_item(self.recipient)
        self.add_item(self.notes)

    async def os_calculation_engine(self):
        total_mats = {}
        set_data = GEAR_DB["gearsets"][self.gearset]
        cost_type = set_data["currency"]
        def add_mats(mat_dict, multiplier=1):
            for mat_name, qty in mat_dict.items():
                total_mats[mat_name] = total_mats.get(mat_name, 0) + (qty * multiplier)

        for piece in self.pieces:
            piece_data = set_data["materials_per_piece"].get(piece, {})
            add_mats(piece_data.get("mats", {}), multiplier=max(1, len(self.jobs)))
            
        for job in self.jobs:
            w_data = GEAR_DB["weapons"].get(job, {})
            if not w_data: continue
            if self.tool_scope in ["both", "mh"]: add_mats(w_data.get("mats_mh", {}))
            if self.tool_scope in ["both", "oh"] and w_data.get("has_offhand", False): add_mats(w_data.get("mats_oh", {}))
                
        mats_display = "\n".join([f"**{qty}x** {name}" for name, qty in total_mats.items()])
        if not mats_display: mats_display = "*No base materials required.*"
        return mats_display, cost_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ **Crunching the numbers and submitting your order...**", ephemeral=True)
        
        try:
            guild = interaction.guild
            mats_list, currency = await self.os_calculation_engine()
            
            tc_payload = []
            items_to_find = []
            set_data = GEAR_DB["gearsets"][self.gearset]
            multiplier = max(1, len(self.jobs))
            
            for piece in self.pieces:
                piece_data = set_data["materials_per_piece"].get(piece, {})
                if "name" in piece_data: items_to_find.append((piece_data["name"], multiplier))
                
            for job in self.jobs:
                w_data = GEAR_DB["weapons"].get(job, {})
                if not w_data: continue
                if self.tool_scope in ["both", "mh"] and "name_mh" in w_data: items_to_find.append((w_data["name_mh"], 1))
                if self.tool_scope in ["both", "oh"] and w_data.get("has_offhand", False) and "name_oh" in w_data: items_to_find.append((w_data["name_oh"], 1))

            async with aiohttp.ClientSession(headers=HEADERS) as session:
                for exact_name, qty in items_to_find:
                    try:
                        safe_name = exact_name.replace('"', '')
                        clean_search = re.sub(r"[^\w\s]", "", safe_name)
                        params_exact = {"sheets": "Item", "query": f'Name~"{safe_name}"', "limit": "1"}
                        params_fuzzy = {"sheets": "Item", "query": clean_search, "limit": "1"}
                        
                        item_id = None
                        async with session.get(f"{XIVAPI_BASE}/search", params=params_exact, timeout=2) as resp:
                            data = await resp.json() if resp.status == 200 else {}
                            if data.get('results'): item_id = data['results'][0].get('row_id')
                        
                        if not item_id and clean_search != safe_name:
                            async with session.get(f"{XIVAPI_BASE}/search", params=params_fuzzy, timeout=2) as resp:
                                data = await resp.json() if resp.status == 200 else {}
                                if data.get('results'): item_id = data['results'][0].get('row_id')
                                
                        if item_id: tc_payload.append((item_id, qty))
                    except Exception:
                        pass
                        
            tc_url = generate_teamcraft_url(tc_payload) if tc_payload else None

            receipt_embed = discord.Embed(title=f"🆕 New Order: {self.gearset}", color=discord.Color.green())
            receipt_embed.add_field(name="Recipient Target", value=self.recipient.value, inline=False)
            receipt_embed.add_field(name="Armor Targets", value=", ".join(self.pieces), inline=True)
            
            if self.jobs:
                job_displays = []
                for job in self.jobs:
                    w_data = GEAR_DB["weapons"].get(job, {})
                    if w_data.get("has_offhand", False):
                        scope_text = "MH+OH" if self.tool_scope == "both" else "MH Only" if self.tool_scope == "mh" else "OH Only"
                        job_displays.append(f"{job} ({scope_text})")
                    else:
                        job_displays.append(job)
                receipt_embed.add_field(name="Job Profiles Included", value=", ".join(job_displays), inline=True)
                
            receipt_embed.add_field(name=f"📦 Required Materials ({currency})", value=mats_list, inline=False)
            if tc_url: receipt_embed.add_field(name="Teamcraft Link", value=f"[🛠️ Open Recipe List]({tc_url})", inline=False)
            if self.notes.value: receipt_embed.add_field(name="📝 Special Notes", value=self.notes.value, inline=False)

            active_orders_channel = discord.utils.get(guild.text_channels, name="open-orders")
            if not active_orders_channel: active_orders_channel = await guild.create_text_channel("open-orders")

            base_message = await active_orders_channel.send(embed=receipt_embed)
            thread = await base_message.create_thread(name=f"Order - {self.recipient.value[:20]}")
            control_msg = await thread.send("Use the dashboard below to coordinate this craft.", view=OrderControlView())

            crafter_role = discord.utils.get(guild.roles, name=CRAFTER_ROLE_NAME)
            role_ping = f"<@&{crafter_role.id}>" if crafter_role else ""
            ping_msg = await thread.send(f"{interaction.user.mention} {role_ping}")
            await ping_msg.delete()
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO orders (control_message_id, root_message_id, requester_id, items_summary, recipient, tc_url) VALUES (?, ?, ?, ?, ?, ?)",
                           (control_msg.id, base_message.id, interaction.user.id, mats_list, self.recipient.value, tc_url))
            conn.commit()
            conn.close()

            await interaction.edit_original_response(content=f"✅ **Order successfully logged to** <#{active_orders_channel.id}>! You can now dismiss this message.")
            
        except Exception as e:
            error_trace = traceback.format_exc()
            print(error_trace)
            await interaction.edit_original_response(content=f"❌ **CRITICAL ERROR in FinalizeOrderModal:**\n```py\n{e}\n```\nTell the admin to check the logs!")

# ==========================================
# CART SYSTEM MODAL & VIEW
# ==========================================
class CartCheckoutModal(discord.ui.Modal, title="Checkout Cart"):
    recipient = discord.ui.TextInput(label="Recipient Character Name", placeholder="Who gets this gear?", required=True)
    notes = discord.ui.TextInput(label="Special Requests / Exceptions", style=discord.TextStyle.paragraph, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ **Assembling your cart and pushing to logistics...**", ephemeral=True)

        try:
            user_data = USER_CARTS.get(interaction.user.id, {})
            cart_items = user_data.get("items", [])
            
            if not cart_items:
                await interaction.edit_original_response(content="❌ Your cart is empty!")
                return

            items_summary_list = []
            tc_payload = []
            for item in cart_items:
                items_summary_list.append(f"• **{item['qty']}x** {item['name']}")
                if item['id'] is not None: tc_payload.append((item['id'], item['qty']))

            items_summary_str = "\n".join(items_summary_list)
            tc_url = generate_teamcraft_url(tc_payload) if tc_payload else None

            guild = interaction.guild
            active_orders_channel = discord.utils.get(guild.text_channels, name="open-orders")
            if not active_orders_channel: active_orders_channel = await guild.create_text_channel("open-orders")

            embed = discord.Embed(title="🆕 Custom Cart Order", color=discord.Color.blue())
            embed.add_field(name="Recipient Target", value=self.recipient.value, inline=False)
            embed.add_field(name="Requested Items", value=items_summary_str, inline=False)
            embed.add_field(name="Requested By", value=interaction.user.mention, inline=True)
            if tc_url: embed.add_field(name="Teamcraft Link", value=f"[🛠️ Open Recipe List]({tc_url})", inline=False)
            if self.notes.value: embed.add_field(name="📝 Special Notes", value=self.notes.value, inline=False)

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

            USER_CARTS.pop(interaction.user.id, None)
            
            await interaction.edit_original_response(content=f"✅ **Cart successfully ordered to** <#{active_orders_channel.id}>! You can now dismiss this message.")
            
        except Exception as e:
            error_trace = traceback.format_exc()
            print(error_trace)
            await interaction.edit_original_response(content=f"❌ **CRITICAL ERROR in CartCheckoutModal:**\n```py\n{e}\n```\nTell the admin to check the logs!")

class CartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900) 

    @discord.ui.button(label="Checkout & Submit ✅", style=discord.ButtonStyle.success, custom_id="checkout_cart")
    async def checkout_cart(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CartCheckoutModal()
        modal.recipient.default = interaction.user.display_name
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Clear Cart 🗑️", style=discord.ButtonStyle.danger, custom_id="clear_cart")
    async def clear_cart(self, interaction: discord.Interaction, button: discord.ui.Button):
        USER_CARTS.pop(interaction.user.id, None)
        await interaction.response.edit_message(content="🛒 Your cart has been cleared.", embed=None, view=None)

# ==========================================
# ORDER CONTROL BUTTONS
# ==========================================
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
            embed.title = embed.title.replace("🆕 New Order", "🛠️ Order Claimed").replace("🆕 Bulk Crafting Order", "🛠️ Order Claimed").replace("🆕 Custom Cart Order", "🛠️ Order Claimed").replace("🆕 Single Item Order", "🛠️ Order Claimed")
            
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
                        await requester.send(f"✅ Your crafting order has been completed by {interaction.user.name}!")
                    except: pass
                
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
        
        cursor.execute("UPDATE orders SET control_message_id=?, root_message_id=?, status='Pending', reminded=0 WHERE control_message_id=?", (control_msg.id, root_msg.id, old_control_id))
        conn.commit()
        conn.close()
        
        await interaction.message.delete()
        await interaction.response.send_message("Order restored to `#open-orders`!", ephemeral=True)

# ==========================================
# SLASH COMMANDS
# ==========================================
@bot.tree.command(name="update_gearsets", description="Upload a new gear_database.json, or leave file blank to download the current one.")
async def update_gearsets(interaction: discord.Interaction, file: discord.Attachment = None):
    crafter_role = discord.utils.get(interaction.guild.roles, name=CRAFTER_ROLE_NAME)
    is_admin = interaction.user.guild_permissions.administrator
    
    if crafter_role not in interaction.user.roles and not is_admin:
        await interaction.response.send_message("❌ Access Denied: You must be a Crafter or Administrator to access the database.", ephemeral=True)
        return
        
    if file is None:
        try:
            current_db = discord.File(JSON_PATH, filename="current_gear_database.json")
            await interaction.response.send_message(
                "📥 **Here is the current active database.**\nDownload this file, make your edits in a text editor, and run this command again with your new file attached to update the bot!", 
                file=current_db, 
                ephemeral=True
            )
        except FileNotFoundError:
            await interaction.response.send_message("❌ Database file not found on the server yet.", ephemeral=True)
        return

    if not file.filename.endswith('.json'):
        await interaction.response.send_message("❌ Invalid format. Please upload a valid `.json` file.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    # -----------------------------------------
    # THE BOUNCER LOGIC
    # -----------------------------------------
    raw_data = await file.read()
    
    try:
        new_db = json.loads(raw_data.decode('utf-8'))
    except json.JSONDecodeError as e:
        await interaction.followup.send(f"❌ **Syntax Error!** Your JSON is broken.\n**Line {e.lineno}, Column {e.colno}:** {e.msg}")
        return
    except UnicodeDecodeError:
        await interaction.followup.send("❌ **Encoding Error!** Please make sure the file is saved as UTF-8.")
        return

    error_msg = validate_database_schema(new_db)
    if error_msg:
        await interaction.followup.send(f"❌ **Schema Error!** The file is valid JSON, but it failed inspection:\n**{error_msg}**")
        return
        
    # -----------------------------------------
    # PASSES INSPECTION - SAVE OVER LIVE DATA
    # -----------------------------------------
    try:
        with open(JSON_PATH, "w", encoding='utf-8') as f:
            json.dump(new_db, f, indent=4)
            
        global GEAR_DB
        GEAR_DB = load_gear_data()
        await interaction.followup.send(f"✅ **Database Validated & Updated!** Successfully reloaded from `{file.filename}`.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to process the database update: {e}")

@bot.tree.command(name="order", description="Add an item to your crafting cart with autocomplete.")
@app_commands.describe(item_name="Search for the item you need to craft (For multiple items, use the Dashboard!)", quantity="How many do you need?")
async def slash_order(interaction: discord.Interaction, item_name: str, quantity: int = 1):
    await interaction.response.defer(ephemeral=True)
    
    item_id = None
    if "|" in item_name:
        parts = item_name.split("|")
        item_id = parts[0]
        official_name = parts[1]
    else:
        official_name = item_name
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
        
    if interaction.user.id not in USER_CARTS:
        USER_CARTS[interaction.user.id] = {"items": [], "last_interaction": None}
        
    old_interaction = USER_CARTS[interaction.user.id].get("last_interaction")
    if old_interaction:
        try:
            await old_interaction.delete_original_response()
        except Exception:
            pass 
            
    USER_CARTS[interaction.user.id]["items"].append({
        "name": official_name,
        "qty": quantity,
        "id": item_id
    })
    
    USER_CARTS[interaction.user.id]["last_interaction"] = interaction
    
    cart_items = USER_CARTS[interaction.user.id]["items"]
    cart_str = "\n".join([f"• **{item['qty']}x** {item['name']}" for item in cart_items])
    
    embed = discord.Embed(title="🛒 Your Crafting Cart", color=discord.Color.blue())
    embed.add_field(name="Current Items", value=cart_str, inline=False)
    embed.set_footer(text="Run /order again to add more items, or click Checkout below to submit!")

    await interaction.followup.send(embed=embed, view=CartView(), ephemeral=True)

@slash_order.autocomplete("item_name")
async def item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not current or len(current) < 3:
        return []
    
    search_url = f"{XIVAPI_BASE}/search"
    safe_current = current.replace('"', '')
    
    params_exact = {"sheets": "Item", "query": f'Name~"{safe_current}"', "limit": "15"}
    clean_search = re.sub(r"[^\w\s]", "", safe_current)
    params_fuzzy = {"sheets": "Item", "query": clean_search, "limit": "15"}
    wildcard_query = " ".join([f"*{word}*" for word in clean_search.split()])
    params_wildcard = {"sheets": "Item", "query": wildcard_query, "limit": "15"}
    
    choices = {}
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        try:
            async with session.get(search_url, params=params_exact, timeout=2) as resp:
                data = await resp.json() if resp.status == 200 else {}
                results = data.get('results', [])
            
            if not results and clean_search != safe_current:
                async with session.get(search_url, params=params_fuzzy, timeout=2) as resp:
                    data = await resp.json() if resp.status == 200 else {}
                    results = data.get('results', [])
            
            if not results:
                async with session.get(search_url, params=params_wildcard, timeout=2) as resp:
                    data = await resp.json() if resp.status == 200 else {}
                    results = data.get('results', [])
            
            for item in results:
                official_name = item['fields'].get('Name', 'Unknown')
                item_id = item.get('row_id') or item.get('id')
                val_str = f"{item_id}|{official_name}"[:100]
                
                if official_name not in choices:
                    choices[official_name] = app_commands.Choice(name=official_name[:100], value=val_str)
                    
        except Exception as e:
            print(f"[Autocomplete Error] {e}")
            
    return list(choices.values())[:25]

# ==========================================
# ERROR HANDLER & SETUP COMMAND
# ==========================================
@bot.event
async def on_command_error(ctx, error):
    print(f"⚠️ Command Error: {error}")
    try:
        await ctx.send(f"❌ **Error executing command:** {error}")
    except discord.Forbidden:
        print("⚠️ I don't even have permission to send the error message in that channel!")

@bot.tree.command(name="ordersetup", description="Deploy the Hearthkeepers Order Board dashboard.")
@app_commands.default_permissions(administrator=True)
async def slash_ordersetup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        guild = interaction.guild
        orders_channel = discord.utils.get(guild.text_channels, name="place-order")
        
        if not orders_channel:
            bot_member = guild.get_member(bot.user.id)
            if not bot_member.guild_permissions.manage_channels:
                await interaction.followup.send("❌ **Error:** I do not have the 'Manage Channels' permission to create the #place-order channel!")
                return
            orders_channel = await guild.create_text_channel("place-order")

        embed = discord.Embed(
            title=" 🛠️Hearthkeepers Order Board 🛠️",
            description="Welcome to the Order Board! \n\nClick **Gearset Wizard** to configure standard tier gearsets with auto-calculated materials.\n\nClick **Items / Gear** to submit custom items or a Teamcraft link.\n\nYou may also use the **/order** command to build and submit a cart.",
            color=discord.Color.purple()
        )
        
        await orders_channel.send(embed=embed, view=DashboardView())
        await interaction.followup.send(f"✅ Dashboard successfully placed down inside <#{orders_channel.id}>!")
        
    except discord.Forbidden:
        await interaction.followup.send("❌ **Permission Error:** Discord blocked me. Make sure my bot role is dragged high up in the Server Settings -> Roles list, and that I have 'Send Messages' and 'Embed Links' in the target channel.")
    except Exception as e:
        await interaction.followup.send(f"❌ **Unexpected Error:** `{e}`")
        print(f"Setup Error: {e}")

# ==========================================
# STARTUP EVENT
# ==========================================
@bot.event
async def on_ready():
    init_db()
    bot.add_view(DashboardView())
    bot.add_view(OrderControlView())
    bot.add_view(ClosedOrderView())
    
    await rebuild_database(bot)
    
    print(f"Logged in safely as {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"Synchronized {len(synced)} application commands.")
    except Exception as e:
        print(f"Failed application sync execution: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)
