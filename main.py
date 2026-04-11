import asyncio
from telethon import TelegramClient, events

# Import our custom modules
import config
import downloader
import handlers

# --- INITIALIZE DUAL CLIENTS ---
# We initialize them here at the top level so the event loop is created
bot = TelegramClient('bot_session', config.API_ID, config.API_HASH).start(bot_token=config.BOT_TOKEN)
userbot = TelegramClient('user_session', config.API_ID, config.API_HASH)

def register_handlers():
    """Registers all bot commands to the main bot client."""
    
    # ⚙️ Task Management & Core
    bot.add_event_handler(handlers.start_handler, events.NewMessage(pattern=r'^/start$'))
    bot.add_event_handler(handlers.cancel_handler, events.NewMessage(pattern=r'^/cancel$'))
    bot.add_event_handler(handlers.delete_handler, events.NewMessage(pattern=r'^/del'))

    # 🗄️ File Manager
    bot.add_event_handler(handlers.fm_handler, events.NewMessage(pattern=r'^/fm(?:\s+(.*))?$'))

    # 🧲 Aria2c Management
    bot.add_event_handler(handlers.aria_handler, events.NewMessage(pattern=r'^/aria (mv|tv|mv2|tv2)(?:\s+(.*))?$'))
    bot.add_event_handler(handlers.aria_manage_handler, events.NewMessage(pattern=r'^/aria (list|start|stop|rm|del)$'))

    # 📥 Standard & Link Downloads
    bot.add_event_handler(handlers.standard_handler, events.NewMessage(pattern=r'^/(mv|tv|mv2|tv2)$'))
    bot.add_event_handler(handlers.link_handler, events.NewMessage(pattern=r'^/l(mv|tv|mv2|tv2)'))


async def main():
    print("Registering event handlers...")
    register_handlers()

    print("Starting Userbot...")
    await userbot.start()  # Will prompt for phone/code on first run if session doesn't exist
    
    print("Starting Bot...")
    # Bot is already started via .start() at the top of the file
    
    print("🚀 Dual-Client System Ready!")

    # Pass the initialized clients to the modules that need them 
    # (This keeps our modules decoupled and prevents circular imports)
    downloader.bot = bot
    downloader.userbot = userbot
    handlers.bot = bot
    handlers.userbot = userbot

    # Start the background download queue worker
    bot.loop.create_task(downloader.download_worker())
    
    # Run both clients simultaneously until you manually stop the script
    await asyncio.gather(
        bot.run_until_disconnected(),
        userbot.run_until_disconnected()
    )


if __name__ == '__main__':
    # Use the bot's native loop to run our main async function
    bot.loop.run_until_complete(main())
    

