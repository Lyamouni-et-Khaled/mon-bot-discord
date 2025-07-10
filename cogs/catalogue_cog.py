

import discord
from discord.ext import commands
from discord import app_commands
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import uuid
import re

# Importation pour l'autocomplétion et la vérification de type
from .manager_cog import ManagerCog
from .manager_cog import TicketCloseView, TicketCreationView


# --- Vues et Modals pour l'Interaction avec le Catalogue ---

class PaymentVerificationView(discord.ui.View):
    def __init__(self, manager: 'ManagerCog'):
        super().__init__(timeout=None)
        self.manager = manager

    async def _handle_action(self, interaction: discord.Interaction, action: str):
        await interaction.response.defer()

        footer_text = interaction.message.embeds[0].footer.text
        match = re.search(r"ID de Transaction: ([a-f0-9-]+)", footer_text)
        if not match:
            return await interaction.followup.send("ID de transaction introuvable dans le message.", ephemeral=True)
        
        transaction_id = match.group(1)

        async with self.manager.data_lock:
            transaction_data = self.manager.pending_actions["transactions"].get(transaction_id)
            if not transaction_data:
                for item in self.children: item.disabled = True
                await interaction.message.edit(view=self)
                return await interaction.followup.send("Cette transaction est introuvable ou a déjà été traitée.", ephemeral=True)

            original_embed = interaction.message.embeds[0]
            new_embed = original_embed.copy()
            
            if action == "confirm":
                product = self.manager.get_product(transaction_data['product_id'])
                option = None
                if transaction_data.get('option_name') and product.get('options'):
                    option = next((opt for opt in product['options'] if opt['name'] == transaction_data['option_name']), None)

                purchase_successful, message = await self.manager.record_purchase(
                    user_id=transaction_data['user_id'],
                    product=product,
                    option=option,
                    credit_used=transaction_data['credit_used'],
                    guild_id=interaction.guild_id,
                    transaction_code=transaction_data.get('transaction_code', 'N/A')
                )

                if not purchase_successful:
                    return await interaction.followup.send(f"❌ Erreur lors de la confirmation: {message}", ephemeral=True)

                new_embed.title = "✅ Commande Validée"
                new_embed.color = discord.Color.green()
                new_embed.clear_fields()
                new_embed.description = f"Le paiement pour le produit `{product['name']}` a été validé."
                new_embed.set_footer(text=f"Validé par {interaction.user.display_name} | {original_embed.footer.text}")
                
                buyer = interaction.guild.get_member(transaction_data['user_id'])
                if buyer:
                    is_subscription = product.get("type") == "subscription"
                    embed_delivery = discord.Embed(
                        title=f"✅ {'Abonnement Activé' if is_subscription else 'Commande Complétée'}",
                        color=discord.Color.green()
                    )
                    if is_subscription:
                        embed_delivery.description = f"Merci pour votre soutien ! Votre abonnement **{product['name']}** est maintenant actif. Profitez de vos avantages exclusifs !"
                    else:
                        embed_delivery.description = f"Merci pour votre achat de **{product['name']}**!\nUn administrateur va vous contacter dans ce ticket pour vous livrer votre produit."
                    
                    await interaction.channel.send(content=f"{buyer.mention}", embed=embed_delivery, view=TicketCloseView(self.manager))

            elif action == "deny":
                new_embed.title = "❌ Paiement Refusé"
                new_embed.color = discord.Color.red()
                new_embed.clear_fields()
                new_embed.description = "La commande a été refusée. Les crédits utilisés ont été remboursés."
                new_embed.set_footer(text=f"Refusé par {interaction.user.display_name} | {original_embed.footer.text}")
                
                await interaction.channel.send(content="Cette commande a été refusée.", view=TicketCloseView(self.manager))


            for item in self.children: item.disabled = True
            await interaction.message.edit(embed=new_embed, view=self)

            del self.manager.pending_actions["transactions"][transaction_id]
            await self.manager._save_json_data_async(self.manager.PENDING_ACTIONS_FILE, self.manager.pending_actions)

    @discord.ui.button(label="✅ Confirmer Paiement", style=discord.ButtonStyle.success, custom_id="confirm_payment_ticket")
    async def confirm_payment_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "confirm")
    
    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger, custom_id="deny_payment_ticket")
    async def deny_payment_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "deny")

class ProductActionView(discord.ui.View):
    def __init__(self, product: Dict, manager: 'ManagerCog', user: discord.User, option: Optional[Dict] = None):
        super().__init__(timeout=300)
        self.product = product
        self.manager = manager
        self.option = option

    @discord.ui.button(label="🛒 Acheter ce produit", style=discord.ButtonStyle.success)
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_purchase_flow(interaction, use_credit=False)
        
    async def start_purchase_flow(self, interaction: discord.Interaction, use_credit: bool = False):
        if self.product.get("options") and not self.option:
            view = OptionSelectView(product=self.product, manager=self.manager)
            await interaction.response.send_message("Ce produit a plusieurs options. Veuillez en choisir une :", view=view, ephemeral=True)
        else:
            await self.create_purchase_ticket(interaction, self.product, self.option, use_credit)

    async def create_purchase_ticket(self, interaction: discord.Interaction, product: Dict, option: Optional[Dict] = None, use_credit: bool = False):
        await interaction.response.defer(ephemeral=True)

        if not self.manager: return await interaction.followup.send("Erreur critique.", ephemeral=True)
        
        base_price = option['price'] if option else product.get('price', 0)
        if base_price < 0:
             return await interaction.followup.send("Ce produit a un prix variable et ne peut être acheté directement. Veuillez contacter le staff.", ephemeral=True)

        final_price = base_price
        is_subscription = product.get("type") == "subscription"
        currency = product.get("currency", "EUR")
        transaction_id = str(uuid.uuid4())
        transaction_code = f"RB-{transaction_id[:4].upper()}"
        
        # --- Embed for the ticket ---
        embed_ticket = discord.Embed(title=f"Nouvelle Commande : {product['name']}", color=discord.Color.gold())
        product_display_name = product['name'] + (f" ({option['name']})" if option else "")
        embed_ticket.description = f"Cette transaction concerne le produit **{product_display_name}**."

        embed_ticket.add_field(name="Utilisateur", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed_ticket.add_field(name="**Total à payer**", value=f"**{final_price:.2f} {currency}**", inline=True)
        
        payment_info = self.manager.config["PAYMENT_INFO"]
        embed_ticket.add_field(
            name="Instructions de paiement",
            value=f"Veuillez envoyer `{final_price:.2f} {currency}` à notre [PayPal.Me]({payment_info['PAYPAL_ME_LINK']}) ou directement à l'adresse `{payment_info['PAYPAL_EMAIL']}`.",
            inline=False
        )
        embed_ticket.add_field(
            name="⚠️ Code de Transaction",
            value=f"Veuillez **IMPÉRATIVEMENT** inclure ce code dans la note de votre paiement PayPal :\n**`{transaction_code}`**",
            inline=False
        )
        embed_ticket.set_footer(text=f"ID de Transaction: {transaction_id}")
        
        # --- Persist transaction data ---
        async with self.manager.data_lock:
            self.manager.pending_actions['transactions'][transaction_id] = {
                "user_id": interaction.user.id,
                "product_id": product['id'],
                "option_name": option['name'] if option else None,
                "credit_used": 0, # Credit system to be added here if needed
                "transaction_code": transaction_code
            }
            await self.manager._save_json_data_async(self.manager.PENDING_ACTIONS_FILE, self.manager.pending_actions)

        # --- Create ticket ---
        ticket_types = self.manager.config.get("TICKET_SYSTEM", {}).get("TICKET_TYPES", [])
        purchase_ticket_type = next((tt for tt in ticket_types if tt.get("label") == "Achat de Produit"), None)

        if not purchase_ticket_type:
             return await interaction.followup.send("Erreur: Le type de ticket 'Achat de Produit' n'est pas configuré.", ephemeral=True)

        ticket_channel = await self.manager.create_ticket(
            user=interaction.user,
            guild=interaction.guild,
            ticket_type=purchase_ticket_type,
            embed=embed_ticket,
            view=PaymentVerificationView(self.manager)
        )
        
        if ticket_channel:
            await interaction.followup.send(f"Votre ticket d'achat a été créé : {ticket_channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send("Impossible de créer le ticket d'achat. Veuillez contacter un administrateur.", ephemeral=True)

class OptionSelect(discord.ui.Select):
    def __init__(self, product: Dict, manager: 'ManagerCog'):
        self.product = product
        self.manager = manager
        currency = product.get("currency", "EUR")
        
        options = [
            discord.SelectOption(
                label=f"{opt['name']} ({opt['price']:.2f} {currency})",
                value=opt['name']
            )
            for opt in product.get('options', [])
        ]
        super().__init__(placeholder="Choisissez une option...", options=options, custom_id=f"option_select:{product['id']}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_option_name = self.values[0]
        selected_option = next((opt for opt in self.product['options'] if opt['name'] == selected_option_name), None)

        if not selected_option:
            return await interaction.followup.send("Option invalide.", ephemeral=True)
            
        action_view = ProductActionView(self.product, self.manager, interaction.user, option=selected_option)
        await action_view.start_purchase_flow(interaction)

class ProductSelect(discord.ui.Select):
    def __init__(self, cog: 'CatalogueCog', products: List[Dict]):
        self.cog = cog
        self.products = products
        options = [discord.SelectOption(label=p['name'][:100], value=p['id']) for p in products]
        super().__init__(placeholder="Choisissez un produit pour voir les détails...", options=options, custom_id="product_select_menu")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        product_id = self.values[0]
        product = self.cog.manager.get_product(product_id)
        if not product:
            return await interaction.edit_original_response(content="Ce produit n'existe plus.", view=None, embed=None)

        embed = self.cog.create_product_embed(product)
        view = self.view # Re-use the parent view
        
        # Remove old action view if it exists
        action_view_item = next((item for item in view.children if isinstance(item, ProductActionView)), None)
        if action_view_item:
            view.remove_item(action_view_item)
            
        # Create and add the correct action view
        if product.get("options"):
            action_view = discord.ui.View(timeout=180)
            action_view.add_item(OptionSelect(product, self.cog.manager))
        else:
            action_view = ProductActionView(product, self.cog.manager, interaction.user)
            
        # This is a bit tricky. We can't add a View to a View. We add its items.
        for item in action_view.children:
            view.add_item(item)
            
        await interaction.edit_original_response(embed=embed, view=view)


class CatalogueBrowseView(discord.ui.View):
    def __init__(self, cog: 'CatalogueCog', categories: List[str]):
        super().__init__(timeout=300)
        self.cog = cog
        self.manager = cog.manager
        
        self.add_item(discord.ui.Select(
            placeholder="Choisissez une catégorie...",
            options=[discord.SelectOption(label=cat) for cat in categories],
            custom_id="category_select_menu"
        ))
        
    @discord.ui.select(custom_id="category_select_menu")
    async def on_category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        category = select.values[0]
        products_in_category = [p for p in self.manager.products if p.get('category') == category]

        # Create a new view for the next step
        new_view = CatalogueBrowseView(self.cog, [opt.label for opt in select.options])
        
        # Remove old product select if it exists
        product_select_item = next((item for item in new_view.children if isinstance(item, ProductSelect)), None)
        if product_select_item:
            new_view.remove_item(product_select_item)
            
        new_view.add_item(ProductSelect(self.cog, products_in_category))
        
        embed = discord.Embed(
            title=f"Catalogue - {category}",
            description="Veuillez sélectionner un produit dans le menu ci-dessous pour afficher ses détails et l'acheter.",
            color=discord.Color.blurple()
        )
        await interaction.edit_original_response(embed=embed, view=new_view)
        
class CatalogueCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager: Optional[ManagerCog] = None

    async def cog_load(self):
        self.manager = self.bot.get_cog('ManagerCog')
        if not self.manager:
            print("ERREUR CRITIQUE: CatalogueCog n'a pas pu trouver le ManagerCog.")
        else:
            self.bot.add_view(PaymentVerificationView(self.manager))

    def get_display_price(self, product: Dict[str, Any]) -> str:
        currency = product.get("currency", "EUR")
        if "options" in product and product.get("options"):
            try:
                prices = [opt['price'] for opt in product['options']]
                min_price = min(prices)
                return f"À partir de `{min_price:.2f} {currency}`"
            except (ValueError, TypeError):
                 return "`Prix variable`"
        elif "price_text" in product:
            return f"`{product['price_text']}`"
        else:
            price = product.get('price', 0.0)
            if price < 0:
                return "`Prix sur demande`"
            return f"`{price:.2f} {currency}`"

    def create_product_embed(self, product: Dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"🛒 {product.get('name', 'Produit sans nom')}",
            description=product.get("description", "Pas de description."),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"ID du produit : {product.get('id')}")
        if product.get("image_url"):
            embed.set_thumbnail(url=product.get("image_url"))
        
        embed.add_field(name="Prix", value=self.get_display_price(product), inline=True)
        embed.add_field(name="Catégorie", value=product.get("category", "N/A"), inline=True)
        return embed
    
    @app_commands.command(name="catalogue", description="Affiche les produits disponibles de manière interactive.")
    async def catalogue(self, interaction: discord.Interaction):
        if not self.manager: return await interaction.response.send_message("Erreur interne.", ephemeral=True)
        
        categories = sorted(list(set(p['category'] for p in self.manager.products)))
        
        view = CatalogueBrowseView(self, categories)
        embed = discord.Embed(
            title="Bienvenue au Catalogue ResellBoost",
            description="Veuillez choisir une catégorie dans le menu déroulant pour commencer.",
            color=discord.Color.purple()
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="produit", description="Affiche les détails d'un produit par son ID.")
    @app_commands.describe(id="L'ID unique du produit (ex: vbucks)")
    async def produit(self, interaction: discord.Interaction, id: str):
        if not self.manager: return await interaction.response.send_message("Erreur interne du bot.", ephemeral=True)
        
        product = self.manager.get_product(id)
        if not product:
            return await interaction.response.send_message("Ce produit est introuvable.", ephemeral=True)

        embed = self.create_product_embed(product)
        
        if product.get("options"):
            view = discord.ui.View(timeout=180)
            view.add_item(OptionSelect(product, self.manager))
        else:
            view = ProductActionView(product, self.manager, interaction.user)
            
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(CatalogueCog(bot))
