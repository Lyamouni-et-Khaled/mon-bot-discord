import os
import asyncio
import discord
from discord.ext import commands
import json
import traceback
from flask import Flask
from threading import Thread

# --- Configuration Globale ---
COGS_TO_LOAD = [
    'cogs.manager_cog',
    'cogs.catalogue_cog',
    'cogs.assistant_cog',
    'cogs.moderator_cog',
    'cogs.giveaway_cog',
    'cogs.guild_cog'
]

BOT_TOKEN = os.environ.get("DISCORD_TOKEN")

class ResellBoostBot(commands.Bot):
    """ Classe personnalisée pour le bot. """
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        intents.invites = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """ Charge les extensions (cogs) au démarrage. """
        print("--- Démarrage du setup_hook ---")
        for cog_name in COGS_TO_LOAD:
            try:
                await self.load_extension(cog_name)
                print(f"✅ Cog '{cog_name}' chargé avec succès.")
            except Exception as e:
                # Si un cog ne se charge pas, on affiche l'erreur et on continue
                print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                print(f"❌ ERREUR LORS DU CHARGEMENT DU COG : {cog_name}")
                print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                traceback.print_exc()

    async def on_ready(self):
        """ Événement appelé lorsque le bot est connecté et prêt. """
        print("-" * 50)
        print(f"Connecté en tant que {self.user} (ID: {self.user.id})")
        print("Le bot est prêt et en ligne.")
        print("-" * 50)

# --- Bloc pour le serveur web (pour Cloud Run) ---
app = Flask('')
@app.route('/')
def home():
    return "Le bot est en ligne."

def run_flask():
  port = int(os.environ.get('PORT', 8080))
  app.run(host='0.0.0.0', port=port)

# --- Point d'entrée principal avec un "piège à erreurs" ---
if __name__ == "__main__":
    # Ce bloc try...except va attraper N'IMPORTE QUELLE erreur
    # qui se produit pendant le démarrage du bot.
    try:
        print("Lancement du service...")

        # 1. Lance le serveur web dans un thread séparé.
        flask_thread = Thread(target=run_flask)
        flask_thread.start()
        print("Serveur web pour le health check démarré.")

        # 2. Lance le bot Discord.
        print("Lancement du bot Discord...")
        if not BOT_TOKEN:
            raise ValueError("ERREUR CRITIQUE: Le token du bot (DISCORD_TOKEN) n'est pas défini.")

        bot = ResellBoostBot()
        bot.run(BOT_TOKEN)

    except Exception as e:
        # Si une erreur se produit, on l'affiche de manière très visible.
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("    UNE ERREUR CRITIQUE A EMPÊCHÉ LE BOT DE DÉMARRER")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        # Affiche l'erreur complète dans les logs
        traceback.print_exc()

