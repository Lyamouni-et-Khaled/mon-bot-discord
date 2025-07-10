
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone
import random
import math
import uuid
from typing import List, Dict, Any, Optional
import aiofiles
import re
import traceback

# Dépendance pour la génération d'image
try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    import io
    IMAGING_AVAILABLE = True
except ImportError:
    IMAGING_AVAILABLE = False


# --- Configuration de l'IA Gemini ---
try:
    import google.generativeai as genai
    from google.generativeai.types import GenerationConfig
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False

# --- Classes pour les Vues d'Interaction ---

class MissionView(discord.ui.View):
    def __init__(self, manager: 'ManagerCog'):
        super().__init__(timeout=None)
        self.manager = manager
    
    @discord.ui.button(label="Activer/Désactiver les notifications de mission", style=discord.ButtonStyle.secondary, custom_id="toggle_mission_dms")
    async def toggle_dms(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        self.manager.initialize_user_data(user_id_str)
        
        current_status = self.manager.user_data[user_id_str].get("missions_opt_in", True)
        new_status = not current_status
        self.manager.user_data[user_id_str]["missions_opt_in"] = new_status
        
        status_text = "activées" if new_status else "désactivées"
        await interaction.response.send_message(f"Vos notifications de mission par message privé sont maintenant {status_text}.", ephemeral=True)
        await self.manager._save_json_data_async(self.manager.USER_DATA_FILE, self.manager.user_data)


class ChallengeSubmissionModal(discord.ui.Modal, title="Soumission de Défi"):
    submission_text = discord.ui.TextInput(
        label="Décrivez comment vous avez complété le défi",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: J'ai aidé @utilisateur à configurer son compte en lui expliquant comment faire...",
        required=True
    )

    def __init__(self, manager: 'ManagerCog', challenge_type: str):
        super().__init__()
        self.manager = manager
        self.challenge_type = challenge_type

    async def on_submit(self, interaction: discord.Interaction):
        await self.manager.handle_challenge_submission(interaction, self.submission_text.value, self.challenge_type)

class CashoutModal(discord.ui.Modal, title="Demande de Retrait d'Argent"):
    amount = discord.ui.TextInput(label="Montant en crédit à retirer", placeholder="Ex: 10.50", required=True)
    paypal_email = discord.ui.TextInput(label="Votre email PayPal", placeholder="Ex: votre.email@example.com", style=discord.TextStyle.short, required=True)

    def __init__(self, manager: 'ManagerCog'):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        await self.manager.handle_cashout_submission(interaction, self.amount.value, self.paypal_email.value)

class CashoutRequestView(discord.ui.View):
    def __init__(self, manager: 'ManagerCog'):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="✅ Approuver", style=discord.ButtonStyle.success, custom_id="approve_cashout")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        msg_id = str(interaction.message.id)
        
        async with self.manager.data_lock:
            cashout_data = self.manager.pending_actions["cashouts"].get(msg_id)
            if not cashout_data:
                button.disabled = True
                self.children[1].disabled = True
                await interaction.message.edit(view=self)
                return await interaction.followup.send("Cette demande de retrait est introuvable ou a déjà été traitée.", ephemeral=True)

            user_id_str = str(cashout_data['user_id'])
            self.manager.initialize_user_data(user_id_str)
            
            await self.manager.add_transaction(user_id_str, "cashout_count", 1, "Approbation de retrait")

            member = interaction.guild.get_member(cashout_data['user_id'])
            if member:
                await self.manager.check_achievements(member)
                try:
                    await member.send(f"✅ Votre demande de retrait de `{cashout_data['euros_to_send']:.2f}€` a été approuvée ! Le paiement sera effectué sous peu sur l'adresse `{cashout_data['paypal_email']}`.")
                except discord.Forbidden: pass
                
            await self.manager.log_public_transaction(
                interaction.guild,
                f"✅ Demande de retrait approuvée pour **{member.display_name if member else 'Utilisateur Inconnu'}**.",
                f"**Montant :** `{cashout_data['euros_to_send']:.2f}€`\n**Validé par :** {interaction.user.mention}",
                discord.Color.green()
            )

            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = "Demande de Retrait APPROUVÉE"
            embed.set_footer(text=f"Approuvé par {interaction.user.display_name}")

            button.disabled = True
            self.children[1].disabled = True
            await interaction.message.edit(embed=embed, view=self)

            del self.manager.pending_actions["cashouts"][msg_id]
            await self.manager._save_json_data_async(self.manager.PENDING_ACTIONS_FILE, self.manager.pending_actions)
        
        await interaction.followup.send("Demande approuvée.", ephemeral=True)


    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger, custom_id="deny_cashout")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        msg_id = str(interaction.message.id)

        async with self.manager.data_lock:
            cashout_data = self.manager.pending_actions["cashouts"].get(msg_id)
            if not cashout_data:
                button.disabled = True
                self.children[0].disabled = True
                await interaction.message.edit(view=self)
                return await interaction.followup.send("Cette demande de retrait est introuvable ou a déjà été traitée.", ephemeral=True)

            user_id_str = str(cashout_data['user_id'])
            self.manager.initialize_user_data(user_id_str)
            
            await self.manager.add_transaction(
                user_id_str,
                "store_credit",
                cashout_data['credit_to_deduct'],
                "Remboursement suite au refus de retrait"
            )
            
            member = interaction.guild.get_member(cashout_data['user_id'])
            if member:
                try:
                    await member.send(f"❌ Votre demande de retrait a été refusée par le staff. Vos `{cashout_data['credit_to_deduct']:.2f}` crédits vous ont été remboursés.")
                except discord.Forbidden: pass
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.red()
            embed.title = "Demande de Retrait REFUSÉE"
            embed.set_footer(text=f"Refusé par {interaction.user.display_name}")

            button.disabled = True
            self.children[0].disabled = True
            await interaction.message.edit(embed=embed, view=self)

            del self.manager.pending_actions["cashouts"][msg_id]
            await self.manager._save_json_data_async(self.manager.PENDING_ACTIONS_FILE, self.manager.pending_actions)

        await interaction.followup.send("Demande refusée et crédits remboursés.", ephemeral=True)


class VerificationView(discord.ui.View):
    def __init__(self, manager: 'ManagerCog'):
        super().__init__(timeout=None)
        self.manager = manager
    
    @discord.ui.button(label="✅ Accepter le règlement", style=discord.ButtonStyle.success, custom_id="verify_member_button")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        verified_role_name = self.manager.config["ROLES"]["VERIFIED"]
        unverified_role_name = self.manager.config["ROLES"]["UNVERIFIED"]
        
        verified_role = discord.utils.get(interaction.guild.roles, name=verified_role_name)
        unverified_role = discord.utils.get(interaction.guild.roles, name=unverified_role_name)

        if not verified_role:
            return await interaction.response.send_message(f"Erreur : Le rôle `{verified_role_name}` est introuvable.", ephemeral=True)
            
        if verified_role in interaction.user.roles:
            return await interaction.response.send_message("Vous êtes déjà vérifié !", ephemeral=True)

        try:
            await interaction.user.add_roles(verified_role, reason="Vérification via bouton")
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role, reason="Vérification via bouton")
            await interaction.response.send_message("Vous avez été vérifié avec succès ! Bienvenue sur le serveur.", ephemeral=True)
            
            # Grant XP to referrer if the new member validates
            user_id_str = str(interaction.user.id)
            self.manager.initialize_user_data(user_id_str)
            user_data = self.manager.user_data[user_id_str]
            if user_data.get("referrer"):
                referrer_id_str = user_data["referrer"]
                self.manager.initialize_user_data(referrer_id_str)
                referrer = interaction.guild.get_member(int(referrer_id_str))
                if referrer:
                    xp_config = self.manager.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]
                    xp_to_add = xp_config["XP_PER_VERIFIED_INVITE"]
                    await self.manager.grant_xp(referrer, xp_to_add, "Parrainage validé")
                    
        except discord.Forbidden:
            await interaction.response.send_message("Je n'ai pas les permissions pour vous donner le rôle. Veuillez contacter un administrateur.", ephemeral=True)

class TicketCreationView(discord.ui.View):
    def __init__(self, manager: 'ManagerCog'):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="🎫 Ouvrir un ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket_button")
    async def create_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket_types = self.manager.config.get("TICKET_SYSTEM", {}).get("TICKET_TYPES", [])
        if not ticket_types:
            return await interaction.response.send_message("Le système de tickets n'est pas correctement configuré.", ephemeral=True)
        
        # Exclude the purchase ticket type from manual creation
        filtered_types = [tt for tt in ticket_types if tt.get("label") != "Achat de Produit"]
        
        await interaction.response.send_message(view=TicketTypeSelect(self.manager, filtered_types), ephemeral=True)

class TicketTypeSelect(discord.ui.View):
    def __init__(self, manager: 'ManagerCog', ticket_types: List[Dict]):
        super().__init__(timeout=180)
        self.manager = manager
        
        options = [
            discord.SelectOption(label=tt['label'], description=tt.get('description'), value=tt['label'])
            for tt in ticket_types
        ]
        self.select_menu = discord.ui.Select(placeholder="Choisissez le type de ticket...", options=options)
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_label = self.select_menu.values[0]
        ticket_type = next(tt for tt in self.manager.config["TICKET_SYSTEM"]["TICKET_TYPES"] if tt['label'] == selected_label)

        initial_embed = discord.Embed(title=f"Ticket : {ticket_type['label']}", description="Veuillez décrire votre problème en détail. Un membre du staff sera bientôt avec vous.", color=discord.Color.blue())
        initial_embed.set_footer(text=f"Ticket créé par {interaction.user.display_name}")

        ticket_channel = await self.manager.create_ticket(
            user=interaction.user, 
            guild=interaction.guild, 
            ticket_type=ticket_type, 
            embed=initial_embed, 
            view=TicketCloseView(self.manager)
        )

        if ticket_channel:
            await interaction.followup.send(f"Votre ticket a été créé : {ticket_channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send("Impossible de créer le ticket. Veuillez contacter un administrateur.", ephemeral=True)
        
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)

class TicketCloseView(discord.ui.View):
    def __init__(self, manager: 'ManagerCog'):
        super().__init__(timeout=None)
        self.manager = manager
    
    @discord.ui.button(label="🔒 Fermer le Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_button")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        channel = interaction.channel
        button.disabled = True
        await interaction.message.edit(view=self)

        await self.manager.log_ticket_closure(interaction, channel)
        
        await channel.delete(reason=f"Ticket fermé par {interaction.user}")

# --- Le Cog Principal ---

class ManagerCog(commands.Cog):
    """Le cerveau du bot, gère la gamification, l'économie et les données utilisateurs."""
    USER_DATA_FILE = 'data/user_data.json'
    CONFIG_FILE = 'config.json'
    PRODUCTS_FILE = 'products.json'
    ACHIEVEMENTS_FILE = 'achievements_config.json'
    KNOWLEDGE_BASE_FILE = 'knowledge_base.json'
    CURRENT_CHALLENGE_FILE = 'data/current_challenge.json'
    PENDING_ACTIONS_FILE = 'data/pending_actions.json'

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data_lock = asyncio.Lock()
        
        self.config = {}
        self.products = []
        self.achievements = []
        self.knowledge_base = {}
        self.user_data = {}
        self.invites_cache = {}
        self.current_challenge: Optional[Dict[str, Any]] = None
        self.pending_actions = {}
        
        if not IMAGING_AVAILABLE:
            print("⚠️ ATTENTION: La librairie 'Pillow' est manquante. La commande /profil utilisera un embed standard.")

        self.model = None
        if not AI_AVAILABLE:
            print("ATTENTION: Le package google-generativeai n'est pas installé. Les fonctionnalités d'IA seront désactivées.")
        else:
            gemini_key = os.environ.get("GEMINI_API_KEY")
            if gemini_key:
                genai.configure(api_key=gemini_key)
                self.model = genai.GenerativeModel('gemini-2.5-flash-preview-04-17')
                print("✅ Modèle Gemini initialisé avec succès.")
            else:
                print("⚠️ ATTENTION: La clé API Gemini (GEMINI_API_KEY) est manquante dans l'environnement. L'IA est désactivée.")

    async def cog_load(self):
        print("Chargement des données du ManagerCog...")
        await self._load_all_data()
        self.bot.add_view(VerificationView(self))
        self.bot.add_view(TicketCreationView(self))
        self.bot.add_view(TicketCloseView(self))
        self.bot.add_view(CashoutRequestView(self))
        self.bot.add_view(MissionView(self))
        self.weekly_leaderboard_task.start()
        self.mission_assignment_task.start()
        self.check_vip_status_task.start()
        self.weekly_coaching_report_task.start()

    def cog_unload(self):
        self.weekly_leaderboard_task.cancel()
        self.mission_assignment_task.cancel()
        self.check_vip_status_task.cancel()
        self.weekly_coaching_report_task.cancel()
        print("ManagerCog déchargé.")

    @commands.Cog.listener()
    async def on_ready(self):
        print("ManagerCog: Le bot est prêt. Finalisation de la configuration...")
        guild_id_str = self.config.get("GUILD_ID")
        if guild_id_str == "VOTRE_VRAI_ID_DE_SERVEUR_ICI" or not guild_id_str:
            print("ATTENTION: GUILD_ID non configuré. De nombreuses fonctionnalités seront désactivées.")
            return

        guild = self.bot.get_guild(int(guild_id_str))
        if guild:
            await self._update_invite_cache(guild)
            print(f"Cache des invitations mis à jour pour la guilde : {guild.name}")
        else:
            print(f"ATTENTION: Guilde avec l'ID {guild_id_str} non trouvée.")

        print("Tâches de fond démarrées via cog_load.")


    async def _load_json_data_async(self, file_path: str) -> any:
        if not os.path.exists(file_path):
            print(f"Fichier {file_path} non trouvé, création d'un fichier vide.")
            dir_name = os.path.dirname(file_path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name)
            default_content = '{}'
            if 'pending_actions' in file_path:
                default_content = '{"transactions": {}, "cashouts": {}}'
            elif 'user_data' in file_path or 'challenge' in file_path:
                default_content = '{}'
            else:
                default_content = '[]'
            
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(default_content)
            return json.loads(default_content)
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                if not content:
                    return {"transactions": {}, "cashouts": {}} if 'pending_actions' in file_path else ({})
                return json.loads(content)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"Erreur lors du chargement de {file_path}: {e}")
            return {} if 'user_data' in file_path else []

    async def _save_json_data_async(self, file_path: str, data: any):
        async with self.data_lock:
            try:
                loop = asyncio.get_running_loop()
                json_string = await loop.run_in_executor(
                    None, lambda: json.dumps(data, indent=2, ensure_ascii=False)
                )
                async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                    await f.write(json_string)
            except Exception as e:
                print(f"Erreur lors de la sauvegarde de {file_path}: {e}")
    
    async def _load_all_data(self):
        tasks = {
            "config": self._load_json_data_async(self.CONFIG_FILE),
            "products": self._load_json_data_async(self.PRODUCTS_FILE),
            "achievements": self._load_json_data_async(self.ACHIEVEMENTS_FILE),
            "knowledge_base": self._load_json_data_async(self.KNOWLEDGE_BASE_FILE),
            "user_data": self._load_json_data_async(self.USER_DATA_FILE),
            "current_challenge": self._load_json_data_async(self.CURRENT_CHALLENGE_FILE),
            "pending_actions": self._load_json_data_async(self.PENDING_ACTIONS_FILE)
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        
        results_dict = dict(zip(tasks.keys(), results))

        for name, result in results_dict.items():
            if isinstance(result, Exception):
                print(f"Erreur critique lors du chargement du fichier pour '{name}': {result}")
                default_val = []
                if name in ['user_data', 'current_challenge', 'pending_actions', 'knowledge_base']:
                    default_val = {}
                setattr(self, name, default_val)
            else:
                 setattr(self, name, result)

        print("Toutes les données de configuration ont été chargées.")
    
    def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        return next((p for p in self.products if p.get('id') == product_id), None)

    async def _parse_gemini_json_response(self, text: str) -> Optional[Dict[str, Any]]:
        """Analyse de manière robuste une réponse JSON potentiellement mal formatée de l'IA."""
        match = re.search(r'```(?:json)?\s*({.*?})\s*```', text, re.DOTALL)
        json_str = match.group(1) if match else text
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"Erreur de décodage JSON: {e}\nTexte reçu: {text}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        
        xp_config = self.config.get("GAMIFICATION_CONFIG", {}).get("XP_SYSTEM", {})
        if len(message.content.split()) < xp_config.get("ANTI_FARM_MIN_WORDS", 0):
            return

        user_id_str = str(message.author.id)
        self.initialize_user_data(user_id_str)
        
        if xp_config.get("ENABLED", False):
            await self.grant_xp(message.author, "message", f"Message dans #{message.channel.name}")
        
        await self.update_mission_progress(message.author, "send_message", 1)


    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot: return
        
        unverified_role_name = self.config.get("ROLES", {}).get("UNVERIFIED")
        if unverified_role_name:
            role = discord.utils.get(member.guild.roles, name=unverified_role_name)
            if role:
                try:
                    await member.add_roles(role, reason="Nouveau membre")
                except discord.Forbidden:
                    print(f"Permissions manquantes pour assigner le rôle '{unverified_role_name}' à {member.name}")

        self.initialize_user_data(str(member.id))
        old_invites = self.invites_cache.get(member.guild.id, {})
        new_invites = await member.guild.invites()
        inviter = None
        for invite in new_invites:
            if invite.code in old_invites and invite.uses > old_invites[invite.code].uses:
                inviter = invite.inviter
                break
        if inviter and inviter.id != member.id:
            user_id_str = str(member.id)
            self.initialize_user_data(str(inviter.id))
            self.user_data[user_id_str]["referrer"] = str(inviter.id)
            
            await self.add_transaction(
                str(inviter.id),
                "referral_count", 1, f"Parrainage de {member.name}"
            )

            print(f"{member.name} a été invité par {inviter.name}")
            await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)

        await self._update_invite_cache(member.guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        await self._update_invite_cache(invite.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        await self._update_invite_cache(invite.guild)

    def initialize_user_data(self, user_id: str):
        if user_id not in self.user_data:
            self.user_data[user_id] = {
                "xp": 0, "level": 1, "weekly_xp": 0, "last_message_timestamp": 0,
                "message_count": 0, "purchase_count": 0, "purchase_total_value": 0.0,
                "achievements": [], "store_credit": 0.0, "warnings": 0,
                "affiliate_sale_count": 0, "affiliate_earnings": 0.0, "referral_count": 0,
                "cashout_count": 0,
                "completed_challenges": [],
                "xp_gated": False,
                "current_prestige_challenge": None,
                "current_personalized_challenge": None,
                "join_timestamp": datetime.now(timezone.utc).timestamp(),
                "weekly_affiliate_earnings": 0.0,
                "affiliate_booster": 0.0,
                "permanent_affiliate_bonus": False,
                "vip_premium": None,
                "transaction_log": [],
                "missions_opt_in": self.config.get("MISSION_SYSTEM", {}).get("OPT_IN_DEFAULT", True),
                "current_daily_mission": None,
                "current_weekly_mission": None
            }
            print(f"Nouvel utilisateur initialisé : {user_id}")
    
    async def add_transaction(self, user_id: str, type: str, amount: float, description: str):
        self.initialize_user_data(user_id)
        user_data = self.user_data[user_id]
        
        if type in user_data:
            user_data[type] += amount
        else:
             user_data[type] = amount
        
        if "transaction_log" not in user_data:
            user_data["transaction_log"] = []
            
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": type,
            "amount": amount,
            "description": description
        }
        user_data["transaction_log"].append(log_entry)
        
        max_log_size = self.config.get("TRANSACTION_LOG_CONFIG", {}).get("MAX_USER_LOG_SIZE", 50)
        if len(user_data["transaction_log"]) > max_log_size:
            user_data["transaction_log"] = user_data["transaction_log"][-max_log_size:]
            
    async def grant_xp(self, user: discord.Member, source: any, reason: str):
        user_id_str = str(user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data[user_id_str]
        
        if isinstance(source, str) and source == "message" and user_data.get("xp_gated", False):
            return

        xp_config = self.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]
        now = datetime.now().timestamp()
        
        xp_to_add = 0
        if source == "message":
            cooldown = xp_config["ANTI_FARM_COOLDOWN_SECONDS"]
            if now - user_data.get("last_message_timestamp", 0) < cooldown: return
            xp_to_add = random.randint(*xp_config["XP_PER_MESSAGE"])
            user_data["last_message_timestamp"] = now
            await self.add_transaction(user_id_str, "message_count", 1, reason)
        elif isinstance(source, int): # Direct XP grant
            xp_to_add = source
        
        if xp_to_add == 0: return

        total_boost = 1.0
        prestige_config = self.config.get("GAMIFICATION_CONFIG", {}).get("PRESTIGE_LEVELS", {})
        for level_str, data in prestige_config.items():
            if user_data['level'] >= int(level_str):
                total_boost += data.get('xp_bonus', 0.0)
        
        vip_config = self.config.get("GAMIFICATION_CONFIG", {}).get("VIP_SYSTEM", {}).get("PREMIUM", {})
        vip_info = user_data.get("vip_premium")
        if vip_info:
            status = vip_info.get("status", "expired")
            is_active = status == "active"
            is_in_grace = status == "grace" and now < vip_info.get("grace_end_timestamp", 0)

            if is_active or is_in_grace:
                consecutive_weeks = vip_info.get("consecutive_weeks", 1)
                consecutive_months = (consecutive_weeks // 4) + 1
                
                boost = 0.0
                for tier in sorted(vip_config.get("XP_BOOST_TIERS",[]), key=lambda x: x['consecutive_months'], reverse=True):
                    if consecutive_months >= tier['consecutive_months']:
                        boost = tier['boost']
                        break
                
                if is_in_grace:
                    boost *= vip_config.get("GRACE_PERIOD_BENEFIT_MULTIPLIER", 0.5)
                
                total_boost += boost
        
        final_xp = int(xp_to_add * total_boost)
        
        await self.add_transaction(user_id_str, "xp", final_xp, reason)
        await self.add_transaction(user_id_str, "weekly_xp", final_xp, f"Gain hebdomadaire: {reason}")
        
        await self.check_level_up(user)
        await self.check_achievements(user)
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)

    async def check_referral_milestones(self, user: discord.Member):
        user_id_str = str(user.id)
        user_data = self.user_data[user_id_str]
        xp_config = self.config.get("GAMIFICATION_CONFIG", {}).get("XP_SYSTEM", {})

        if not user_data.get("referrer"): return
        
        referrer_id_str = user_data["referrer"]
        self.initialize_user_data(referrer_id_str)
        referrer = user.guild.get_member(int(referrer_id_str))
        if not referrer: return

        if user_data["level"] >= 5 and not user_data.get("lvl5_milestone_rewarded"):
            join_ts = user_data.get("join_timestamp", 0)
            limit_days = xp_config.get("REFERRAL_LVL_5_DAYS_LIMIT", 7)
            if (datetime.now(timezone.utc).timestamp() - join_ts) < (limit_days * 86400):
                xp_gain = xp_config["XP_BONUS_REFERRAL_HITS_LVL_5"]
                await self.grant_xp(referrer, xp_gain, f"Filleul {user.display_name} a atteint le niveau 5")
                user_data["lvl5_milestone_rewarded"] = True
                try:
                    await referrer.send(f"🚀 Votre filleul {user.mention} a atteint le niveau 5 rapidement ! Vous gagnez **{xp_gain} XP** bonus !")
                except discord.Forbidden: pass

    async def check_level_up(self, user: discord.Member):
        user_id_str = str(user.id)
        user_data = self.user_data[user_id_str]

        if user_data.get("xp_gated", False): return

        xp_config = self.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]
        base_xp = xp_config["LEVEL_UP_FORMULA_BASE_XP"]
        multiplier = xp_config["LEVEL_UP_FORMULA_MULTIPLIER"]
        old_level = user_data["level"]
        
        target_level = old_level
        while user_data["xp"] >= int(base_xp * (multiplier ** target_level)):
            target_level += 1

        if target_level == old_level: return

        prestige_config = self.config.get("GAMIFICATION_CONFIG", {}).get("PRESTIGE_LEVELS", {})
        hit_gate = False
        for level_str, challenge_data in sorted(prestige_config.items(), key=lambda x: int(x[0])):
            prestige_level = int(level_str)
            if old_level < prestige_level <= target_level:
                await self.add_transaction(user_id_str, "level", prestige_level - user_data["level"], f"Atteinte du palier de prestige {prestige_level}")
                user_data["xp_gated"] = True
                user_data["current_prestige_challenge"] = challenge_data
                
                dm_embed = discord.Embed(
                    title=f"🏆 Palier de Prestige Atteint : Niveau {prestige_level} !",
                    description=f"Félicitations {user.mention} ! Tu as atteint un jalon important. Pour continuer ta progression, tu dois accomplir un défi spécial.",
                    color=discord.Color.dark_gold()
                )
                dm_embed.add_field(
                    name="Ton Défi de Prestige",
                    value=challenge_data['description'] + "\n\nUtilise la commande `/prestige` pour revoir ce défi ou `/soumettre_defi` lorsque tu l'as complété.",
                    inline=False
                )
                try: await user.send(embed=dm_embed)
                except discord.Forbidden: pass
                hit_gate = True
                break
        
        if not hit_gate and target_level > old_level:
             await self.add_transaction(user_id_str, "level", target_level - old_level, "Montée de niveau")
            
        new_level = user_data["level"]
        
        await self.check_referral_milestones(user)

        channel_name = self.config["CHANNELS"]["LEVEL_UP_ANNOUNCEMENTS"]
        channel = discord.utils.get(user.guild.text_channels, name=channel_name)
        if channel:
            await channel.send(f"🎉 Bravo {user.mention}, tu as atteint le niveau **{new_level}** !")

        try:
            embed_dm = discord.Embed(
                title=f"🎉 Félicitations, tu as atteint le niveau {new_level} !",
                description="Ton activité a payé ! Voici tes récompenses et tes prochains objectifs.",
                color=discord.Color.gold()
            )
            
            reward_text = "Aucune nouvelle récompense de rôle pour ce niveau."
            level_rewards = self.config.get("GAMIFICATION_CONFIG", {}).get("LEVEL_REWARDS", {})
            for level_str, reward_data in level_rewards.items():
                if old_level < int(level_str) <= new_level:
                    if reward_data.get("type") == "role":
                        role_name = reward_data.get("value")
                        reward_text = f"Tu as obtenu le rôle **{role_name}** !"
                        role_to_add = discord.utils.get(user.guild.roles, name=role_name)
                        if role_to_add and role_to_add not in user.roles:
                            await user.add_roles(role_to_add, reason=f"Récompense de niveau {new_level}")
            embed_dm.add_field(name="🎁 Récompense de Rôle", value=reward_text, inline=False)
            
            next_aff_tier = next((t for t in sorted(self.config["GAMIFICATION_CONFIG"]["AFFILIATE_SYSTEM"]["COMMISSION_TIERS"], key=lambda x: x['level']) if new_level < t['level']), None)
            
            motivation_text = "Continue comme ça pour débloquer encore plus d'avantages !"
            if next_aff_tier:
                motivation_text += f"\n- **Au niveau {next_aff_tier['level']}** : Ta commission d'affiliation passera à **{next_aff_tier['rate']*100:.0f}%** !"

            embed_dm.add_field(name="🚀 Prochains Objectifs", value=motivation_text, inline=False)
            
            await user.send(embed=embed_dm)
        except (discord.Forbidden, Exception) as e:
            print(f"Erreur lors de l'envoi du DM de level up: {e}")

        await self.check_achievements(user)
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)


    async def check_achievements(self, user: discord.Member):
        user_id_str = str(user.id)
        user_stats = self.user_data[user_id_str]
        for achievement in self.achievements:
            if achievement["id"] in user_stats.get("achievements", []): continue
            trigger = achievement["trigger"]
            trigger_type = trigger["type"]
            trigger_value = trigger["value"]
            user_value = user_stats.get(trigger_type, 0)
            if user_value >= trigger_value:
                await self.grant_achievement(user, achievement)
    
    async def grant_achievement(self, user: discord.Member, achievement: dict):
        user_id_str = str(user.id)
        self.user_data[user_id_str]["achievements"].append(achievement["id"])
        
        xp_reward = achievement.get("reward_xp", 0)
        await self.grant_xp(user, xp_reward, f"Succès: {achievement['name']}")
        
        channel_name = self.config["CHANNELS"]["ACHIEVEMENT_ANNOUNCEMENTS"]
        channel = discord.utils.get(user.guild.text_channels, name=channel_name)
        if channel:
            embed = discord.Embed(title="🏆 Nouveau Succès Débloqué !", description=f"Félicitations {user.mention} pour avoir débloqué le succès **{achievement['name']}** !", color=discord.Color.gold())
            embed.add_field(name="Description", value=achievement['description'], inline=False)
            embed.add_field(name="Récompense", value=f"{xp_reward} XP", inline=False)
            await channel.send(embed=embed)
        print(f"Succès '{achievement['name']}' accordé à {user.name}")
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
        
    async def record_purchase(self, user_id: int, product: dict, option: Optional[dict], credit_used: float, guild_id: int, transaction_code: str) -> tuple[bool, str]:
        user_id_str = str(user_id)
        self.initialize_user_data(user_id_str)
        guild = self.bot.get_guild(guild_id)
        if not guild: return False, "Guilde non trouvée."
        member = guild.get_member(user_id)
        if not member: return False, "Membre non trouvé."
        
        price = option['price'] if option else product.get('price', 0)
        product_display_name = product['name'] + (f" ({option['name']})" if option else "")

        if product.get("type") == "subscription":
            await self.handle_vip_purchase(member, product)
            return True, "Abonnement enregistré."

        await self.add_transaction(user_id_str, "purchase_count", 1, f"Achat: {product_display_name}")
        await self.add_transaction(user_id_str, "purchase_total_value", price, f"Achat: {product_display_name}")
        
        if credit_used > 0:
            await self.add_transaction(user_id_str, "store_credit", -credit_used, f"Achat avec crédit: {product_display_name}")
        
        xp_per_eur = self.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]["XP_PER_EURO_SPENT"]
        xp_from_purchase = int(price * xp_per_eur)
        await self.grant_xp(member, xp_from_purchase, f"Achat: {product_display_name}")
        
        await self.log_public_transaction(
            guild,
            f"🛒 **{member.display_name}** a acheté `{product_display_name}`.",
            f"**Code :** `{transaction_code}`\n**Valeur :** `{price:.2f} {product.get('currency', 'EUR')}`",
            discord.Color.blue()
        )
        
        referrer_id_str = self.user_data[user_id_str].get("referrer")
        if referrer_id_str:
            self.initialize_user_data(referrer_id_str)
            referrer = guild.get_member(int(referrer_id_str))
            if referrer:
                purchase_cost = product.get('purchase_cost', 0.0)
                if option and 'purchase_cost' in option:
                    purchase_cost = option.get('purchase_cost', 0.0)

                commissionable_amount = price
                if product.get('margin_type') == 'net' and purchase_cost >= 0:
                    commissionable_amount = max(0, price - purchase_cost)
                
                affiliate_config = self.config["GAMIFICATION_CONFIG"]["AFFILIATE_SYSTEM"]
                referrer_data = self.user_data[referrer_id_str]
                referrer_level = referrer_data.get('level', 1)
                
                base_rate = 0.0
                for tier in sorted(affiliate_config["COMMISSION_TIERS"], key=lambda x: x['level'], reverse=True):
                    if referrer_level >= tier['level']:
                        base_rate = tier['rate']
                        break
                
                total_rate = base_rate + referrer_data.get('affiliate_booster', 0.0)
                
                if referrer_data.get("permanent_affiliate_bonus"):
                    total_rate += affiliate_config.get("PERMANENT_LOYALTY_BONUS", {}).get("RATE", 0.0)

                vip_config = self.config.get("GAMIFICATION_CONFIG", {}).get("VIP_SYSTEM", {}).get("PREMIUM", {})
                vip_info = referrer_data.get("vip_premium")
                if vip_info:
                    status = vip_info.get("status", "expired")
                    now_ts = datetime.now(timezone.utc).timestamp()
                    is_active = status == "active"
                    is_in_grace = status == "grace" and now_ts < vip_info.get("grace_end_timestamp", 0)

                    if is_active or is_in_grace:
                        consecutive_weeks = vip_info.get("consecutive_weeks", 1)
                        consecutive_months = (consecutive_weeks // 4) + 1
                        
                        bonus = 0.0
                        for tier in sorted(vip_config.get("COMMISSION_BONUS_TIERS",[]), key=lambda x: x['consecutive_months'], reverse=True):
                            if consecutive_months >= tier['consecutive_months']:
                                bonus = tier['bonus']
                                break
                        
                        if is_in_grace:
                            bonus *= vip_config.get("GRACE_PERIOD_BENEFIT_MULTIPLIER", 0.5)
                        
                        total_rate += bonus

                commission_earned = commissionable_amount * total_rate
                await self.add_transaction(referrer_id_str, "store_credit", commission_earned, f"Commission sur achat de {member.display_name}")
                await self.add_transaction(referrer_id_str, "affiliate_earnings", commission_earned, f"Commission sur achat de {member.display_name}")
                await self.add_transaction(referrer_id_str, "weekly_affiliate_earnings", commission_earned, f"Commission sur achat de {member.display_name}")
                await self.add_transaction(referrer_id_str, "affiliate_sale_count", 1, f"Vente via {member.display_name}")
                
                await self.log_public_transaction(
                    guild,
                    f"🤝 **{referrer.display_name}** a gagné une commission d'affiliation !",
                    f"**Montant :** `{commission_earned:.2f}` crédits\n**Filleul :** `{member.display_name}`",
                    discord.Color.purple()
                )

                try:
                    await referrer.send(f"🎉 Bonne nouvelle ! Votre filleul {member.display_name} a fait un achat. Vous avez gagné **{commission_earned:.2f} crédits** (Taux: {total_rate*100:.1f}%)!")
                except discord.Forbidden: pass
                await self.check_achievements(referrer)
                await self.update_mission_progress(referrer, "affiliate_sale", 1)
                await self.update_mission_progress(referrer, "affiliate_earn", commission_earned)

        
        await self.check_achievements(member)
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
        return True, "Achat enregistré avec succès."

    async def handle_vip_purchase(self, user: discord.Member, product: dict):
        user_id_str = str(user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data[user_id_str]
        
        vip_config = self.config.get("GAMIFICATION_CONFIG", {}).get("VIP_SYSTEM", {}).get("PREMIUM", {})
        role = discord.utils.get(user.guild.roles, name=vip_config.get("ROLE_NAME"))
        if role:
            try: await user.add_roles(role, reason="Achat abonnement VIP Premium")
            except discord.Forbidden: print(f"Impossible d'ajouter le role VIP à {user.name}")
        
        now = datetime.now(timezone.utc)
        duration = timedelta(days=vip_config.get("DURATION_DAYS", 7))
        
        current_vip_data = user_data.get("vip_premium")
        consecutive_weeks = 1
        
        if current_vip_data:
            renewal_deadline = current_vip_data.get("renewal_end_timestamp", 0)
            if renewal_deadline > 0 and now.timestamp() <= renewal_deadline:
                consecutive_weeks = current_vip_data.get("consecutive_weeks", 0) + 1
                end_date = datetime.fromtimestamp(current_vip_data["end_timestamp"], tz=timezone.utc) + duration
            else:
                end_date = now + duration
        else:
            end_date = now + duration

        user_data["vip_premium"] = {
            "status": "active",
            "end_timestamp": end_date.timestamp(),
            "consecutive_weeks": consecutive_weeks,
            "grace_end_timestamp": None,
            "renewal_end_timestamp": None
        }
        
        if user_data.get("referrer"):
            referrer_id = user_data["referrer"]
            self.initialize_user_data(referrer_id)
            referrer = user.guild.get_member(int(referrer_id))
            if referrer:
                xp_bonus = self.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]["XP_BONUS_REFERRAL_BUYS_VIP"]
                await self.grant_xp(referrer, xp_bonus, f"Filleul {user.display_name} a acheté le VIP")
                try: await referrer.send(f"💎 Votre filleul {user.mention} a souscrit au VIP Premium ! Vous gagnez **{xp_bonus} XP** !")
                except discord.Forbidden: pass
        
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
        
    async def handle_cashout_submission(self, interaction: discord.Interaction, amount_str: str, paypal_email: str):
        try: amount = float(amount_str)
        except ValueError: return await interaction.response.send_message("Le montant doit être un nombre.", ephemeral=True)
        user_id_str = str(interaction.user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data[user_id_str]
        cashout_config = self.config["GAMIFICATION_CONFIG"]["CASHOUT_SYSTEM"]
        if not cashout_config["ENABLED"]: return await interaction.response.send_message("Le système de retrait est actuellement désactivé.", ephemeral=True)
        
        if (datetime.now(timezone.utc).timestamp() - user_data.get("join_timestamp", 0)) < (cashout_config["MINIMUM_ACCOUNT_AGE_DAYS"] * 86400):
            return await interaction.response.send_message(f"Votre compte doit avoir au moins {cashout_config['MINIMUM_ACCOUNT_AGE_DAYS']} jours.", ephemeral=True)
        if user_data["level"] < cashout_config["MINIMUM_LEVEL"]:
             return await interaction.response.send_message(f"Vous devez être au moins niveau {cashout_config['MINIMUM_LEVEL']} pour faire un retrait.", ephemeral=True)

        min_threshold = float('inf')
        for tier in sorted(cashout_config["WITHDRAWAL_THRESHOLDS"], key=lambda x: x['level'], reverse=True):
            if user_data['level'] >= tier['level']:
                min_threshold = tier['threshold']
                break
        if amount < min_threshold: return await interaction.response.send_message(f"Le montant minimum de retrait pour votre niveau est de {min_threshold} crédits.", ephemeral=True)
        
        if amount > user_data["store_credit"]: return await interaction.response.send_message("Vous n'avez pas assez de crédits.", ephemeral=True)
        
        euros_to_send = amount * cashout_config["CREDIT_TO_EUR_RATE"]
        
        await self.add_transaction(user_id_str, "store_credit", -amount, "Demande de retrait")
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
        
        channel_name = self.config["CHANNELS"]["CASHOUT_REQUESTS"]
        channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)
        if not channel: return await interaction.response.send_message("Erreur: Canal de requêtes de retrait non trouvé.", ephemeral=True)
        
        embed = discord.Embed(title="Nouvelle Demande de Retrait", color=discord.Color.blue(), timestamp=datetime.now())
        embed.add_field(name="Membre", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="Montant (Crédit)", value=f"`{amount:.2f}`", inline=True)
        embed.add_field(name="Montant (EUR)", value=f"`{euros_to_send:.2f}`", inline=True)
        embed.add_field(name="Email PayPal", value=f"`{paypal_email}`", inline=False)
        
        msg = await channel.send(embed=embed, view=CashoutRequestView(self))

        async with self.data_lock:
            self.pending_actions['cashouts'][str(msg.id)] = {
                "user_id": interaction.user.id,
                "credit_to_deduct": amount,
                "euros_to_send": euros_to_send,
                "paypal_email": paypal_email
            }
            await self._save_json_data_async(self.PENDING_ACTIONS_FILE, self.pending_actions)

        await interaction.response.send_message("Votre demande de retrait a été envoyée au staff pour validation. Le crédit a été déduit de votre compte et sera remboursé si la demande est refusée.", ephemeral=True)

    @tasks.loop(hours=24)
    async def mission_assignment_task(self):
        if not self.config.get("MISSION_SYSTEM", {}).get("ENABLED"):
            return

        print("Début de la tâche d'assignation des missions...")
        guild = self.bot.get_guild(int(self.config["GUILD_ID"]))
        if not guild: return

        mission_config = self.config["MISSION_SYSTEM"]
        daily_templates = [m for m in mission_config.get("TEMPLATES", []) if m["type"] == "daily"]
        weekly_templates = [m for m in mission_config.get("TEMPLATES", []) if m["type"] == "weekly"]
        is_weekly_reset_day = datetime.now(timezone.utc).weekday() == 0

        for user_id_str, user_data in list(self.user_data.items()):
            if not user_data.get("missions_opt_in", False): continue
            
            member = guild.get_member(int(user_id_str))
            if not member or member.bot: continue

            if daily_templates:
                template = random.choice(daily_templates)
                target = random.randint(*template["target_range"])
                reward = random.randint(*template["reward_xp_range"])
                user_data["current_daily_mission"] = {
                    "id": template["id"],
                    "description": template["description"].format(target=target),
                    "target": target, "progress": 0, "reward_xp": reward, "completed": False
                }

            if is_weekly_reset_day and weekly_templates:
                template = random.choice(weekly_templates)
                target = random.randint(*template["target_range"])
                reward = random.randint(*template["reward_xp_range"])
                user_data["current_weekly_mission"] = {
                    "id": template["id"],
                    "description": template["description"].format(target=target),
                    "target": target, "progress": 0, "reward_xp": reward, "completed": False
                }
            
            try:
                embed = discord.Embed(title="📜 Vos Nouvelles Missions", color=discord.Color.purple())
                if user_data.get("current_daily_mission"):
                    daily = user_data["current_daily_mission"]
                    embed.add_field(name="☀️ Mission Quotidienne", value=f"{daily['description']}\n**Récompense :** `{daily['reward_xp']}` XP", inline=False)
                if user_data.get("current_weekly_mission"):
                    weekly = user_data["current_weekly_mission"]
                    embed.add_field(name="📅 Mission Hebdomadaire", value=f"{weekly['description']}\n**Récompense :** `{weekly['reward_xp']}` XP", inline=False)
                
                embed.set_footer(text="Utilisez /missions pour voir votre progression ou désactiver ces messages.")
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                print(f"Impossible d'envoyer les missions en DM à {member.display_name}")

        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
        print("Tâche d'assignation des missions terminée.")

    async def update_mission_progress(self, user: discord.Member, action_id: str, value: float):
        user_id_str = str(user.id)
        if user_id_str not in self.user_data: return

        missions_to_check = ["current_daily_mission", "current_weekly_mission"]
        for mission_key in missions_to_check:
            mission = self.user_data[user_id_str].get(mission_key)
            if mission and not mission.get("completed") and mission.get("id") == action_id:
                mission["progress"] = min(mission["progress"] + value, mission["target"])
                if mission["progress"] >= mission["target"]:
                    mission["completed"] = True
                    await self.grant_xp(user, mission["reward_xp"], f"Mission complétée: {mission['description']}")
                    try:
                        await user.send(f"🎉 **Mission accomplie !**\n> {mission['description']}\nVous avez gagné **{mission['reward_xp']} XP** !")
                    except discord.Forbidden: pass
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)


    @tasks.loop(hours=168)
    async def weekly_leaderboard_task(self):
        guild_id = int(self.config.get("GUILD_ID", 0))
        guild = self.bot.get_guild(guild_id)
        if not guild: return
        
        print("Début de la tâche de classement hebdomadaire...")
        
        xp_leaderboard_data = {uid: data['weekly_xp'] for uid, data in self.user_data.items() if data.get('weekly_xp', 0) > 0}
        sorted_xp_leaderboard = sorted(xp_leaderboard_data.items(), key=lambda item: item[1], reverse=True)
        
        roles_config = self.config.get("ROLES", {})
        top_xp_roles_names = {1: "LEADERBOARD_TOP_1_XP", 2: "LEADERBOARD_TOP_2_XP", 3: "LEADERBOARD_TOP_3_XP"}
        top_xp_roles = {rank: discord.utils.get(guild.roles, name=roles_config.get(role_name)) for rank, role_name in top_xp_roles_names.items()}
        all_top_xp_roles = [r for r in top_xp_roles.values() if r is not None]

        for member in guild.members:
            if any(role in member.roles for role in all_top_xp_roles):
                await member.remove_roles(*all_top_xp_roles, reason="Réinitialisation du classement hebdo XP")

        xp_winners_text = []
        for i, (user_id, xp) in enumerate(sorted_xp_leaderboard[:3]):
            rank = i + 1
            member = guild.get_member(int(user_id))
            if member:
                role_to_add = top_xp_roles.get(rank)
                if role_to_add: await member.add_roles(role_to_add, reason=f"Top {rank} XP hebdo")
                xp_winners_text.append(f"{'🥇🥈🥉'[rank-1]} **{member.display_name}** avec {int(xp)} XP")
        
        aff_config = self.config["GAMIFICATION_CONFIG"]["AFFILIATE_SYSTEM"]
        aff_winners_text = []
        if aff_config.get("WEEKLY_BOOSTERS", {}).get("ENABLED"):
            aff_leaderboard_data = {uid: data['weekly_affiliate_earnings'] for uid, data in self.user_data.items() if data.get('weekly_affiliate_earnings', 0) > 0}
            sorted_aff_leaderboard = sorted(aff_leaderboard_data.items(), key=lambda item: item[1], reverse=True)
            
            for uid in self.user_data: self.user_data[uid]['affiliate_booster'] = 0.0

            boosters = {1: aff_config["WEEKLY_BOOSTERS"]["TOP_1_BOOST"], 2: aff_config["WEEKLY_BOOSTERS"]["TOP_2_BOOST"], 3: aff_config["WEEKLY_BOOSTERS"]["TOP_3_BOOST"]}
            for i, (user_id, earnings) in enumerate(sorted_aff_leaderboard[:3]):
                rank = i + 1
                self.user_data[user_id]['affiliate_booster'] = boosters[rank]
                member = guild.get_member(int(user_id))
                if member:
                     aff_winners_text.append(f"{'🥇🥈🥉'[rank-1]} **{member.display_name}** avec {earnings:.2f} crédits (boost de **+{boosters[rank]*100:.0f}%** pour la semaine)!")

        channel_name = self.config["CHANNELS"]["WEEKLY_LEADERBOARD_ANNOUNCEMENTS"]
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel:
            embed = discord.Embed(title="🏆 Récompenses Hebdomadaires ! 🏆", description="Félicitations aux champions de la semaine !", color=discord.Color.gold())
            if xp_winners_text: embed.add_field(name="Podium XP", value="\n".join(xp_winners_text), inline=False)
            if aff_winners_text: embed.add_field(name="Podium Affiliation", value="\n".join(aff_winners_text), inline=False)
            if xp_winners_text or aff_winners_text: await channel.send(embed=embed)

        for uid in self.user_data:
            self.user_data[uid]['weekly_xp'] = 0
            self.user_data[uid]['weekly_affiliate_earnings'] = 0
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
        print("Tâche de classement hebdomadaire terminée.")

    @tasks.loop(hours=24)
    async def check_vip_status_task(self):
        now = datetime.now(timezone.utc)
        guild_id_str = self.config.get("GUILD_ID")
        if not guild_id_str: return
        guild = self.bot.get_guild(int(guild_id_str))
        if not guild: return
        
        vip_config = self.config.get("GAMIFICATION_CONFIG", {}).get("VIP_SYSTEM", {}).get("PREMIUM", {})
        aff_config = self.config.get("GAMIFICATION_CONFIG", {}).get("AFFILIATE_SYSTEM", {})
        
        premium_role = discord.utils.get(guild.roles, name=vip_config.get("ROLE_NAME"))
        loyalty_role = discord.utils.get(guild.roles, name=aff_config.get("PERMANENT_LOYALTY_BONUS", {}).get("ROLE_NAME"))
        
        if not premium_role:
            print("ATTENTION: Rôle VIP Premium introuvable. La tâche de vérification VIP est suspendue.")
            return

        for user_id_str, user_data in list(self.user_data.items()):
            vip_info = user_data.get("vip_premium")
            if not vip_info: continue

            member = guild.get_member(int(user_id_str))
            if not member: continue
            
            status = vip_info.get("status", "expired")
            now_ts = now.timestamp()

            if status == "active" and now_ts > vip_info.get("end_timestamp", 0):
                vip_info["status"] = "grace"
                grace_duration = timedelta(days=vip_config.get("GRACE_PERIOD_DAYS", 7))
                renewal_duration = timedelta(days=vip_config.get("RENEWAL_WINDOW_DAYS", 3))
                
                grace_end_time = now + grace_duration
                renewal_end_time = grace_end_time + renewal_duration
                
                vip_info["grace_end_timestamp"] = grace_end_time.timestamp()
                vip_info["renewal_end_timestamp"] = renewal_end_time.timestamp()
                
                try:
                    await member.send(f"⚠️ Votre abonnement VIP Premium a expiré. Vous entrez dans une période de grâce de {grace_duration.days} jours avec des avantages réduits. Renouvelez avant la fin pour ne pas briser votre série !")
                except discord.Forbidden: pass
            
            elif status == "grace" and now_ts > vip_info.get("renewal_end_timestamp", 0):
                vip_info["status"] = "expired"
                if premium_role in member.roles:
                    await member.remove_roles(premium_role, reason="Abonnement VIP Premium expiré.")
                
                if loyalty_role and vip_info.get("consecutive_weeks", 0) > 0 and not user_data.get("permanent_affiliate_bonus"):
                    user_data["permanent_affiliate_bonus"] = True
                    await member.add_roles(loyalty_role, reason="Fin d'abonnement VIP Premium.")
                    try:
                        await member.send("Votre abonnement VIP Premium est terminé. En remerciement de votre soutien, vous avez obtenu le rôle **Bonus de Fidélité**, vous octroyant un bonus de commission permanent !")
                    except discord.Forbidden: pass
                else:
                    try:
                        await member.send("Votre abonnement VIP Premium et sa période de renouvellement sont terminés. Vous n'avez plus accès à ses avantages.")
                    except discord.Forbidden: pass
        
        await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)

    @tasks.loop(hours=168)
    async def weekly_coaching_report_task(self):
        """Sends a personalized weekly coaching report to active users."""
        if not self.model: return

        guild = self.bot.get_guild(int(self.config["GUILD_ID"]))
        if not guild: return
        print("Début de la tâche de coaching hebdomadaire...")
        
        prompt_template = self.config.get("AI_PROCESSING_CONFIG", {}).get("AI_WEEKLY_COACH_PROMPT")
        if not prompt_template: return print("Prompt de coaching hebdo manquant.")

        for user_id_str, user_data in list(self.user_data.items()):
            if user_data.get("weekly_xp", 0) == 0 and user_data.get("weekly_affiliate_earnings", 0) == 0:
                continue

            member = guild.get_member(int(user_id_str))
            if not member or member.bot: continue

            try:
                prompt = prompt_template.format(
                    username=member.display_name,
                    weekly_xp=int(user_data.get('weekly_xp', 0)),
                    weekly_affiliate_earnings=f"{user_data.get('weekly_affiliate_earnings', 0.0):.2f}"
                )
                response = await self.model.generate_content_async(prompt)
                await member.send(response.text)
                await asyncio.sleep(1) # To avoid rate limits
            except (discord.Forbidden, discord.HTTPException):
                print(f"Impossible d'envoyer le rapport de coaching à {member.display_name}")
            except Exception as e:
                print(f"Erreur Gemini (Coaching) pour {member.display_name}: {e}")
        
        print("Tâche de coaching hebdomadaire terminée.")

    @weekly_leaderboard_task.before_loop
    @mission_assignment_task.before_loop
    @check_vip_status_task.before_loop
    @weekly_coaching_report_task.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()
    
    async def _get_overwrites_from_config(self, guild: discord.Guild, perms_config: Dict[str, Any], roles_by_name: Dict[str, discord.Role]) -> Dict[discord.Role, discord.PermissionOverwrite]:
        overwrites = {}
        for role_name, perms in perms_config.items():
            target = None
            if role_name == "@everyone":
                target = guild.default_role
            else:
                target = roles_by_name.get(role_name)
            
            if target:
                overwrites[target] = discord.PermissionOverwrite(**perms)
        return overwrites
    
    @app_commands.command(name="setup", description="Crée les rôles et canaux, et les remplit avec l'IA.")
    @app_commands.default_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        report = ["**Rapport de configuration du serveur :**"]
        
        try:
            report.append("\n**--- Rôles ---**")
            role_config = self.config.get("SERVER_SETUP_CONFIG", {}).get("ROLES", [])
            roles_by_name = {r.name: r for r in guild.roles}
            for role_data in role_config:
                if role_data["name"] not in roles_by_name:
                    try:
                        perms = discord.Permissions(**role_data.get("permissions", {}))
                        color_val = role_data.get("color", "0x000000")
                        color = discord.Color(int(color_val, 16))
                        new_role = await guild.create_role(
                            name=role_data["name"], permissions=perms, color=color, 
                            hoist=role_data.get("hoist", False), reason="Setup IA"
                        )
                        roles_by_name[new_role.name] = new_role
                        report.append(f"✅ Rôle **{role_data['name']}** créé.")
                    except Exception as e:
                        report.append(f"❌ Erreur création rôle **{role_data['name']}**: {e}")
                else:
                    report.append(f"☑️ Rôle **{role_data['name']}** existe déjà.")
            
            await asyncio.sleep(1)
            
            report.append("\n**--- Catégories et Canaux ---**")
            category_config = self.config.get("SERVER_SETUP_CONFIG", {}).get("CATEGORIES", {})
            ai_prompt_template = self.config.get("AI_PROCESSING_CONFIG", {}).get("AI_CHANNEL_SETUP_PROMPT")

            for cat_name, cat_data in category_config.items():
                try:
                    overwrites_conf = cat_data.get("permissions", {})
                    for staff_role_name in self.config.get("ROLES", {}).get("STAFF", []):
                        if staff_role_name not in overwrites_conf:
                            overwrites_conf[staff_role_name] = {"view_channel": True}
                    overwrites = await self._get_overwrites_from_config(guild, overwrites_conf, roles_by_name)

                    category = discord.utils.get(guild.categories, name=cat_name)
                    if not category:
                        category = await guild.create_category(cat_name, overwrites=overwrites, reason="Setup IA")
                        report.append(f"✅ Catégorie **{cat_name}** créée.")
                    else:
                        await category.edit(overwrites=overwrites, reason="Synchro configuration")
                        report.append(f"☑️ Catégorie **{cat_name}** synchronisée.")
                    
                    for chan_data in cat_data.get("channels", []):
                        chan_name = chan_data['name']
                        chan_type = chan_data.get('type', 'text')
                        channel = discord.utils.get(guild.channels, name=chan_name)

                        if not channel:
                            chan_overwrites = await self._get_overwrites_from_config(guild, chan_data.get("permissions", {}), roles_by_name)
                            if chan_type == 'forum':
                                channel = await category.create_forum(chan_name, overwrites=chan_overwrites, reason="Setup IA")
                            else:
                                channel = await category.create_text_channel(chan_name, overwrites=chan_overwrites, reason="Setup IA")
                            report.append(f"  ✅ Canal **#{chan_name}** ({chan_type}) créé.")
                            
                            # --- AI Content Generation ---
                            if self.model and ai_prompt_template:
                                content_to_generate = None
                                data_for_ai = {}
                                topic_for_ai = ""

                                if chan_name == self.config["CHANNELS"]["RULES"]:
                                    topic_for_ai = "Règlement du serveur"
                                    data_for_ai = self.config.get("SERVER_RULES", {})
                                elif chan_name == self.config["CHANNELS"]["VERIFICATION"]:
                                    topic_for_ai = "Message de vérification"
                                    data_for_ai = self.config.get("VERIFICATION_SYSTEM", {})
                                
                                if topic_for_ai and data_for_ai:
                                    try:
                                        prompt = ai_prompt_template.format(topic=topic_for_ai, data_json=json.dumps(data_for_ai, ensure_ascii=False))
                                        response = await self.model.generate_content_async(prompt)
                                        content_to_generate = response.text
                                    except Exception as e:
                                        report.append(f"    ⚠️ Erreur IA pour #{chan_name}: {e}")
                                
                                if content_to_generate:
                                    view_to_add = None
                                    if chan_name == self.config["CHANNELS"]["VERIFICATION"]:
                                        view_to_add = VerificationView(self)
                                    
                                    await channel.send(content_to_generate, view=view_to_add)
                                    report.append(f"    🤖 Contenu IA généré pour **#{chan_name}**.")

                        else: # Channel exists
                            if channel.category != category:
                                await channel.edit(category=category, sync_permissions=True, reason="Setup IA")
                                report.append(f"  ➡️ Canal **#{chan_name}** déplacé vers **{cat_name}**.")
                            else:
                                report.append(f"  ☑️ Canal **#{chan_name}** existe déjà.")

                except Exception as e:
                    report.append(f"❌ Erreur catégorie/canal **{cat_name}**: {e}")
                    traceback.print_exc()

            final_report = "\n".join(report)
            if len(final_report) > 1900: final_report = final_report[:1900] + "\n... (rapport tronqué)"
            await interaction.followup.send(f"Configuration terminée.\n```md\n{final_report}\n```", ephemeral=True)

        except Exception as e:
            report.append(f"\n\n❌ **ERREUR CRITIQUE PENDANT LE SETUP**: {e}")
            traceback.print_exc()
            final_report = "\n".join(report)
            if len(final_report) > 1900: final_report = final_report[:1900] + "\n... (rapport tronqué)"
            await interaction.followup.send(f"Une erreur est survenue.\n```md\n{final_report}\n```", ephemeral=True)

    @app_commands.command(name="sync_commandes", description="[Admin] Force la synchronisation des commandes slash avec Discord.")
    @app_commands.default_permissions(administrator=True)
    async def sync_commands(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await self.bot.tree.sync(guild=interaction.guild)
            await interaction.followup.send(f"✅ Synchronisé {len(synced)} commande(s).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Une erreur est survenue lors de la synchronisation : {e}", ephemeral=True)

    @app_commands.command(name="profil", description="Affiche votre profil de gamification, XP et niveau.")
    @app_commands.describe(membre="Le membre dont vous voulez voir le profil (optionnel).")
    async def profil(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        target_user = membre or interaction.user
        if target_user.bot:
            return await interaction.response.send_message("Les bots n'ont pas de profil.", ephemeral=True)

        await interaction.response.defer()

        user_id_str = str(target_user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data[user_id_str]

        if IMAGING_AVAILABLE and self.config.get("PROFILE_CARD_CONFIG"):
            try:
                img = await self.generate_profile_image(target_user, user_data)
                return await interaction.followup.send(file=img)
            except Exception as e:
                print(f"Erreur lors de la génération de l'image de profil: {e}\n{traceback.format_exc()}")
        
        embed = discord.Embed(
            title=f"Profil de {target_user.display_name}",
            color=target_user.color
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.add_field(name="Niveau", value=user_data.get('level', 1), inline=True)
        embed.add_field(name="XP Total", value=int(user_data.get('xp', 0)), inline=True)
        embed.add_field(name="Crédits", value=f"{user_data.get('store_credit', 0.0):.2f} 💰", inline=True)
        
        xp_config = self.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]
        xp_needed = int(xp_config["LEVEL_UP_FORMULA_BASE_XP"] * (xp_config["LEVEL_UP_FORMULA_MULTIPLIER"] ** user_data.get('level', 1)))
        
        embed.add_field(name="Progression", value=f"{int(user_data.get('xp', 0))} / {xp_needed} XP", inline=False)
        
        await interaction.followup.send(embed=embed)

    async def generate_profile_image(self, user: discord.Member, user_data: dict) -> discord.File:
        card_config = self.config.get("PROFILE_CARD_CONFIG")
        
        def get_palette_for_level(level, config):
            if not config: return None
            selected_palette = config.get('DEFAULT_PALETTE')
            sorted_palettes = sorted(config.get('LEVEL_PALETTES', []), key=lambda x: x['level'], reverse=True)
            for tier in sorted_palettes:
                if level >= tier['level']:
                    selected_palette = tier['palette']
                    break
            return selected_palette

        palette = get_palette_for_level(user_data.get('level', 1), card_config)

        W, H = (900, 300)
        img = Image.new('RGB', (W, H), color=palette['background'])
        draw = ImageDraw.Draw(img, 'RGBA')

        draw.rounded_rectangle((20, 20, W-20, H-20), radius=20, fill=palette['surface'])

        try:
            font_bold = ImageFont.truetype("assets/Inter-Bold.ttf", 36)
            font_regular = ImageFont.truetype("assets/Inter-Regular.ttf", 24)
            font_small = ImageFont.truetype("assets/Inter-Regular.ttf", 18)
        except IOError:
            font_bold = ImageFont.load_default()
            font_regular = ImageFont.load_default()
            font_small = ImageFont.load_default()

        try:
            async with self.bot.http._session.get(user.display_avatar.with_format("png").url) as resp:
                if resp.status == 200:
                    avatar_data = await resp.read()
                    avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA")
                    size = (128, 128)
                    mask = Image.new('L', size, 0)
                    draw_mask = ImageDraw.Draw(mask)
                    draw_mask.ellipse((0, 0) + size, fill=255)
                    avatar = ImageOps.fit(avatar, mask.size, centering=(0.5, 0.5))
                    avatar.putalpha(mask)
                    img.paste(avatar, (60, (H - size[1]) // 2), avatar)
        except Exception as e:
            print(f"Impossible de charger l'avatar: {e}")

        draw.text((220, 50), user.display_name, font=font_bold, fill=palette['text'])
        
        xp_config = self.config["GAMIFICATION_CONFIG"]["XP_SYSTEM"]
        current_xp = int(user_data.get('xp', 0))
        level = user_data.get('level', 1)
        xp_for_next_level = int(xp_config["LEVEL_UP_FORMULA_BASE_XP"] * (xp_config["LEVEL_UP_FORMULA_MULTIPLIER"] ** level))
        xp_for_current_level = int(xp_config["LEVEL_UP_FORMULA_BASE_XP"] * (xp_config["LEVEL_UP_FORMULA_MULTIPLIER"] ** (level - 1))) if level > 1 else 0
        
        xp_in_level = current_xp - xp_for_current_level
        xp_needed_for_level = xp_for_next_level - xp_for_current_level
        
        progress = xp_in_level / xp_needed_for_level if xp_needed_for_level > 0 else 1
        progress = min(max(progress, 0), 1)

        draw.text((W - 60, 55), f"LVL {level}", font=font_bold, fill=palette['accent'], anchor="ra")
        
        bar_x, bar_y, bar_w, bar_h = 220, 180, 620, 30
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=15, fill=palette['background'])
        if progress > 0:
            draw.rounded_rectangle((bar_x, bar_y, bar_x + (bar_w * progress), bar_y + bar_h), radius=15, fill=palette['accent'])

        xp_text = f"{xp_in_level} / {xp_needed_for_level} XP"
        draw.text((225, 225), xp_text, font=font_small, fill=palette['text'])
        
        credits_text = f"Crédits: {user_data.get('store_credit', 0.0):.2f}"
        draw.text((W - 60, 225), credits_text, font=font_small, fill=palette['text'], anchor="ra")
        
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return discord.File(buffer, filename=f"profile_{user.id}.png")

    @app_commands.command(name="classement", description="Affiche les classements hebdomadaires.")
    async def classement(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        xp_leaderboard = sorted(
            [ (uid, data.get('weekly_xp', 0)) for uid, data in self.user_data.items() if data.get('weekly_xp', 0) > 0 ],
            key=lambda x: x[1], reverse=True
        )[:10]

        aff_leaderboard = sorted(
            [ (uid, data.get('weekly_affiliate_earnings', 0)) for uid, data in self.user_data.items() if data.get('weekly_affiliate_earnings', 0) > 0 ],
            key=lambda x: x[1], reverse=True
        )[:10]

        embed = discord.Embed(title="🏆 Classements de la Semaine", color=discord.Color.gold())
        
        xp_desc = []
        for rank, (uid, xp) in enumerate(xp_leaderboard):
            member = interaction.guild.get_member(int(uid))
            xp_desc.append(f"{'🥇🥈🥉'[rank] if rank < 3 else rank+1} {member.display_name if member else 'Utilisateur Inconnu'} - **{int(xp)} XP**")
        embed.add_field(name="Top XP", value="\n".join(xp_desc) if xp_desc else "Aucune activité cette semaine.", inline=False)
        
        aff_desc = []
        for rank, (uid, earnings) in enumerate(aff_leaderboard):
            member = interaction.guild.get_member(int(uid))
            aff_desc.append(f"{'🥇🥈🥉'[rank] if rank < 3 else rank+1} {member.display_name if member else 'Utilisateur Inconnu'} - **{earnings:.2f} crédits**")
        embed.add_field(name="Top Affiliation", value="\n".join(aff_desc) if aff_desc else "Aucune commission gagnée cette semaine.", inline=False)
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="missions", description="Consultez vos missions en cours.")
    async def missions(self, interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data[user_id_str]
        embed = discord.Embed(title=f"Missions de {interaction.user.display_name}", color=discord.Color.dark_purple())
        
        has_missions = False
        if user_data.get("current_daily_mission") and not user_data["current_daily_mission"].get("completed"):
            mission = user_data["current_daily_mission"]
            embed.add_field(name="☀️ Mission Quotidienne", value=f"> {mission['description']}\n`{mission['progress']}/{mission['target']}` - **{mission['reward_xp']} XP**", inline=False)
            has_missions = True
        
        if user_data.get("current_weekly_mission") and not user_data["current_weekly_mission"].get("completed"):
            mission = user_data["current_weekly_mission"]
            embed.add_field(name="📅 Mission Hebdomadaire", value=f"> {mission['description']}\n`{mission['progress']}/{mission['target']}` - **{mission['reward_xp']} XP**", inline=False)
            has_missions = True
            
        if not has_missions:
            embed.description = "Vous avez accompli toutes vos missions, ou de nouvelles missions vous seront bientôt attribuées. Revenez plus tard !"

        await interaction.response.send_message(embed=embed, view=MissionView(self), ephemeral=True)

    @app_commands.command(name="mon_defi", description="Obtenez un défi personnalisé généré par l'IA juste pour vous.")
    async def my_challenge(self, interaction: discord.Interaction):
        if not self.model:
            return await interaction.response.send_message("Le coach IA n'est pas disponible pour le moment.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        user_id_str = str(interaction.user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data[user_id_str]

        if user_data.get("current_personalized_challenge"):
            challenge = user_data["current_personalized_challenge"]
            embed = discord.Embed(title=f" défi personnalisé en cours",
                                  description=f"Vous avez déjà un défi personnalisé actif !\n\n**{challenge['title']}**\n> {challenge['description']}\n\nTerminez-le avant d'en demander un nouveau. Utilisez `/soumettre_defi`.",
                                  color=discord.Color.orange())
            return await interaction.followup.send(embed=embed, ephemeral=True)

        prompt_template = self.config.get("AI_PROCESSING_CONFIG", {}).get("AI_PERSONALIZED_CHALLENGE_PROMPT")
        if not prompt_template:
            return await interaction.followup.send("Le coach IA ne parvient pas à trouver d'inspiration...", ephemeral=True)
            
        user_stats_for_prompt = {
            "level": user_data["level"],
            "xp_total": user_data["xp"],
            "xp_hebdomadaire": user_data["weekly_xp"],
            "gains_affiliation_total": user_data.get("affiliate_earnings", 0),
            "nombre_filleuls": user_data.get("referral_count", 0)
        }
        
        prompt = prompt_template.format(user_stats=json.dumps(user_stats_for_prompt))
        
        try:
            generation_config = GenerationConfig(response_mime_type="application/json")
            response = await self.model.generate_content_async(prompt, generation_config=generation_config)
            challenge_data = await self._parse_gemini_json_response(response.text)
            
            if not challenge_data or not all(k in challenge_data for k in ["title", "description", "xp_reward"]):
                 return await interaction.followup.send("L'IA a eu une panne d'inspiration, réessayez plus tard.", ephemeral=True)

            user_data["current_personalized_challenge"] = challenge_data
            await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)
            
            embed = discord.Embed(title=f"💡 Votre nouveau défi : {challenge_data['title']}", color=discord.Color.blue())
            embed.description = challenge_data['description']
            embed.add_field(name="Récompense", value=f"**{challenge_data['xp_reward']} XP**", inline=True)
            embed.add_field(name="Difficulté", value=challenge_data.get('difficulty', 'N/A'), inline=True)
            embed.set_footer(text="Utilisez /soumettre_defi lorsque vous avez terminé !")
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"Erreur Gemini (Défi Perso): {e}")
            await interaction.followup.send("Une erreur est survenue avec le coach IA. Veuillez réessayer.", ephemeral=True)

    @app_commands.command(name="prestige", description="Consultez votre défi de prestige.")
    async def prestige(self, interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        self.initialize_user_data(user_id_str)
        user_data = self.user_data.get(user_id_str, {})
        if not user_data.get("xp_gated") or not user_data.get("current_prestige_challenge"):
            return await interaction.response.send_message("Vous n'avez pas de défi de prestige actif.", ephemeral=True)
        
        challenge = user_data["current_prestige_challenge"]
        embed = discord.Embed(
            title=f"🏆 Défi de Prestige : {challenge['name']}",
            description=challenge['description'],
            color=discord.Color.dark_gold()
        )
        embed.set_footer(text="Utilisez /soumettre_defi pour valider.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="soumettre_defi", description="Soumettez une preuve pour votre défi (prestige ou personnalisé).")
    async def submit_challenge(self, interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        user_data = self.user_data.get(user_id_str, {})
        
        challenge_type = None
        if user_data.get("xp_gated"):
            challenge_type = "prestige"
        elif user_data.get("current_personalized_challenge"):
            challenge_type = "personalized"
        else:
            return await interaction.response.send_message("Vous n'avez pas de défi actif à soumettre.", ephemeral=True)
            
        await interaction.response.send_modal(ChallengeSubmissionModal(self, challenge_type))

    @app_commands.command(name="cashout", description="Faites une demande de retrait de vos crédits.")
    async def cashout(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CashoutModal(self))
    
    async def handle_challenge_submission(self, interaction: discord.Interaction, submission_text: str, challenge_type: str):
        await interaction.response.defer(ephemeral=True)

        if not self.model:
            return await interaction.followup.send("Le juge IA est actuellement indisponible. Veuillez contacter le staff.", ephemeral=True)
        
        user_id_str = str(interaction.user.id)
        user_data = self.user_data[user_id_str]
        
        challenge_desc = ""
        if challenge_type == "prestige":
            challenge_desc = user_data.get("current_prestige_challenge", {}).get("description", "Défi de prestige non trouvé.")
        elif challenge_type == "personalized":
            challenge_desc = user_data.get("current_personalized_challenge", {}).get("description", "Défi personnalisé non trouvé.")

        prompt_template = self.config.get("AI_PROCESSING_CONFIG", {}).get("AI_CHALLENGE_VALIDATION_PROMPT")
        prompt = prompt_template.format(challenge_description=challenge_desc, submission_text=submission_text)

        try:
            generation_config = GenerationConfig(response_mime_type="application/json")
            response = await self.model.generate_content_async(prompt, generation_config=generation_config)
            result = await self._parse_gemini_json_response(response.text)

            if not result or not all(k in result for k in ["is_valid", "justification", "xp_reward"]):
                raise Exception("Réponse JSON invalide de l'IA.")

            if result["is_valid"]:
                await self.grant_xp(interaction.user, result["xp_reward"], f"Défi {challenge_type} validé par IA")
                
                # Reset the specific challenge
                if challenge_type == "prestige":
                    user_data["xp_gated"] = False
                    user_data["current_prestige_challenge"] = None
                elif challenge_type == "personalized":
                    user_data["current_personalized_challenge"] = None
                
                await self._save_json_data_async(self.USER_DATA_FILE, self.user_data)

                embed = discord.Embed(title="✅ Défi Validé !", color=discord.Color.green())
                embed.description = f"Le juge IA a validé votre soumission :\n> *{result['justification']}*"
                embed.add_field(name="Récompense", value=f"**+{result['xp_reward']} XP**")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
                # Announce prestige completion publicly
                if challenge_type == 'prestige':
                    announce_chan = discord.utils.get(interaction.guild.text_channels, name=self.config['CHANNELS']['ACHIEVEMENT_ANNOUNCEMENTS'])
                    if announce_chan:
                        await announce_chan.send(f"🏆 **{interaction.user.mention}** a bravé les épreuves et a complété son défi de prestige ! Sa progression continue !")
            else:
                embed = discord.Embed(title="❌ Défi Refusé", color=discord.Color.red())
                embed.description = f"Le juge IA a analysé votre soumission et a décidé de ne pas la valider pour le moment :\n> *{result['justification']}*"
                embed.set_footer(text="N'hésitez pas à améliorer votre preuve et à la soumettre à nouveau !")
                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"Erreur lors de la validation IA du défi: {e}")
            await interaction.followup.send("Une erreur est survenue lors de la communication avec le juge IA. Votre soumission sera validée manuellement par le staff.", ephemeral=True)
            # Fallback to manual validation
            channel_name = self.config["CHANNELS"]["STAFF_CHAT"]
            channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)
            if channel:
                embed = discord.Embed(title=f"⚠️ Validation Manuelle Requise (Erreur IA)", color=discord.Color.orange())
                embed.add_field(name="Utilisateur", value=interaction.user.mention)
                embed.add_field(name="Défi", value=challenge_desc, inline=False)
                embed.add_field(name="Preuve", value=submission_text, inline=False)
                await channel.send(embed=embed)


    async def _update_invite_cache(self, guild: discord.Guild):
        try:
            self.invites_cache[guild.id] = {invite.code: invite for invite in await guild.invites()}
        except discord.Forbidden:
            print(f"Permissions manquantes pour lister les invitations dans la guilde {guild.name}")

    async def log_public_transaction(self, guild: discord.Guild, title: str, description: str, color: discord.Color):
        if not self.config.get("TRANSACTION_LOG_CONFIG",{}).get("ENABLED"): return
        channel_name = self.config["CHANNELS"]["TRANSACTION_LOGS"]
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel:
            embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
            await channel.send(embed=embed)
    
    async def create_ticket(self, user: discord.Member, guild: discord.Guild, ticket_type: dict, embed: discord.Embed, view: discord.ui.View):
        ticket_config = self.config["TICKET_SYSTEM"]
        category_name = ticket_config["TICKET_CATEGORY_NAME"]
        category = discord.utils.get(guild.categories, name=category_name)
        if not category:
            try:
                staff_roles = self.config.get("ROLES", {}).get("STAFF", [])
                overwrites = { guild.default_role: discord.PermissionOverwrite(view_channel=False) }
                for role_name in staff_roles:
                    role = discord.utils.get(guild.roles, name=role_name)
                    if role: overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                category = await guild.create_category(category_name, overwrites=overwrites, reason="Catégorie pour les tickets")
            except discord.Forbidden:
                 print("Impossible de créer la catégorie de ticket.")
                 return None

        ping_role_name = ticket_type.get("ping_role")
        ping_role = discord.utils.get(guild.roles, name=ping_role_name) if ping_role_name else None
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
        }
        
        support_role_names = self.config.get("ROLES", {}).get("STAFF", []) + self.config.get("ROLES", {}).get("SUPPORT", [])
        for role_name in list(set(support_role_names)):
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True)
        
        channel_name = f"{ticket_type['label'].lower().replace(' ', '-')}-{user.name}"
        
        try:
            ticket_channel = await guild.create_text_channel(
                channel_name, category=category, overwrites=overwrites,
                topic=f"Ticket pour {user.name} ({user.id}). Type: {ticket_type['label']}",
                reason=f"Création de ticket pour {user.name}"
            )
        except discord.Forbidden:
            print("Impossible de créer le canal de ticket.")
            return None
        
        ping_content = ping_role.mention if ping_role else ""
        
        await ticket_channel.send(content=f"Bienvenue {user.mention} ! {ping_content}", embed=embed, view=view)
        return ticket_channel

    async def log_ticket_closure(self, interaction: discord.Interaction, channel: discord.TextChannel):
        log_channel_name = self.config["CHANNELS"]["TICKET_LOGS"]
        log_channel = discord.utils.get(interaction.guild.text_channels, name=log_channel_name)
        if not log_channel: return
        
        transcript_messages = []
        async for msg in channel.history(limit=100, oldest_first=True):
            transcript_messages.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {msg.author.display_name}: {msg.content}")
        
        transcript = "\n".join(transcript_messages)
        transcript_file = discord.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")
        
        ai_summary_text = "IA non disponible pour le résumé."
        if self.model:
            summary_prompt = self.config.get("TICKET_SYSTEM", {}).get("AI_SUMMARY_PROMPT", "")
            if summary_prompt and transcript:
                try:
                    prompt = summary_prompt.format(transcript=transcript[-3000:])
                    response = await self.model.generate_content_async(contents=prompt)
                    ai_summary_text = response.text
                except Exception as e:
                    ai_summary_text = f"Erreur lors du résumé IA: {e}"
            else:
                ai_summary_text = "Prompt de résumé IA non configuré ou transcript vide."
        
        embed = discord.Embed(title=f"Log de Ticket Fermé : {channel.name}", color=discord.Color.greyple())
        embed.add_field(name="Fermé par", value=interaction.user.mention, inline=True)
        embed.add_field(name="Résumé IA", value=f"```json\n{ai_summary_text[:1000]}\n```", inline=False)
        
        await log_channel.send(embed=embed, file=transcript_file)

async def setup(bot: commands.Bot):
    await bot.add_cog(ManagerCog(bot))
