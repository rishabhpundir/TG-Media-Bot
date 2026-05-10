import os
import json
import time
import random
import asyncio
import logging
from pytz import timezone
from telethon.tl import types
from dotenv import load_dotenv
from telethon import TelegramClient, errors 

"""
Example :
TELEGRAM_API_ID=1315489
TELEGRAM_API_HASH=a1c0b751f16c82dfb88c3ef4b5cc600a
CHAT_ID=5503171843
DESTINATION_CHANNEL_ID=2440772255
"""

# Load environment variables
load_dotenv()


# Setup Logging
LOG_FILE = "backup.log"
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),  # Log to file
        logging.StreamHandler()  # Log to console
    ]
)
logger = logging.getLogger(__name__)

# Config & Credentials
SESSION_NAME = 'Chat_Backup'
CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID"))
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")
DESTINATION_CHANNEL_ID = int(os.environ.get("CHAT_BACKUP_CHANNEL_ID"))
PROGRESS_FILE = "backup_progress.json"

# Batch settings
FETCH_LIMIT = 500  # Number of messages to fetch per request
BATCH_SIZE = 20  # Messages per batch
RETRY_DELAY = 70  # Wait time if rate-limited


class TelegramChatBackup:
    def load_last_message_id(self):
        """Load last processed message ID from file."""
        try:
            with open(PROGRESS_FILE, "r") as f:
                data = json.load(f)
                last_message_id = data.get("last_message_id", None)
                total_processed = data.get("total_processed", None)
                return last_message_id, total_processed
        except (FileNotFoundError, json.JSONDecodeError):
            return None, None

    def save_last_message_id(self, message_id, total_processed):
        """Save last processed message ID to file."""
        with open(PROGRESS_FILE, "w") as f:
            json.dump({"last_message_id": message_id, "total_processed": total_processed}, f)

    async def backup_chat(self):
        async with TelegramClient(SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH) as client:
            me = await client.get_me()
            logger.info(f"Logged in as {me.username}")

            last_message_id, total_processed = self.load_last_message_id()  # Get last processed message ID, Total Processed
            logger.info(f"Resuming from message ID: {last_message_id}" if last_message_id else "Starting fresh...")
            
            offset_id = last_message_id or 0
            total_processed = total_processed or 0
            
            while True:
                try:
                    messages = []
                    logger.info(f"Fetching messages after {offset_id}...")
                    async for message in client.iter_messages(CHAT_ID, limit=FETCH_LIMIT, reverse=True, offset_id=offset_id):
                        IST = timezone("Asia/Kolkata")
                        msg_time = message.date.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")  # Convert to IST
                        sender = (await message.get_sender()).username or "Nidhi"
                        text = message.text or "[Media]"  # Handle media separately
                        formatted_msg = f"{msg_time} - {sender}: {text}"
                        messages.append((message.id, formatted_msg, message.media))

                    if not messages:
                        logger.info("No more messages to process. Backup complete!")
                        break  # Exit loop if there are no more messages

                    total_messages = len(messages)
                    logger.info(f"Fetched {total_messages} messages.")

                    # Send messages in batches
                    for i in range(0, total_messages, BATCH_SIZE):
                        DELAY_BETWEEN_BATCHES = random.randint(12, 20)
                        batch = messages[i : i + BATCH_SIZE]
                        batch_texts = "\n".join(msg[1] for msg in batch)
                        last_msg_id = batch[-1][0]  # Get last processed message ID
                        try:
                            await client.send_message(DESTINATION_CHANNEL_ID, batch_texts)
                            logger.info(f"Sent batch {i // BATCH_SIZE + 1}/{total_messages // BATCH_SIZE + 1}")
                            for msg_id, _, media in batch:
                                if media:
                                    await client.send_message(DESTINATION_CHANNEL_ID, f"[Media from {msg_id}]")
                                    if isinstance(media, types.MessageMediaGeoLive):
                                        await client.send_message(DESTINATION_CHANNEL_ID, f"📍 Live Location shared at message {msg_id}")
                                    elif isinstance(media, types.MessageMediaWebPage):
                                        webpage = media.webpage
                                        if hasattr(webpage, 'url'):
                                            await client.send_message(DESTINATION_CHANNEL_ID, f"🌐 Webpage shared: {webpage.url}")
                                        else:
                                            await client.send_message(DESTINATION_CHANNEL_ID, f"🌐 Webpage preview at message {msg_id}")
                                    else:
                                        await client.send_file(DESTINATION_CHANNEL_ID, media)

                            # Save progress after each batch
                            self.save_last_message_id(last_msg_id, total_processed)
                            offset_id = last_msg_id  # Update offset to continue from here

                        except errors.FloodWaitError as e:
                            logger.warning(f"Rate-limited! Waiting {e.seconds} seconds...")
                            time.sleep(e.seconds + DELAY_BETWEEN_BATCHES)
                        except errors.RPCError as e:
                            logger.error(f"RPC Error: {e}, retrying after {RETRY_DELAY} sec...")
                            time.sleep(RETRY_DELAY)

                        logger.info(f"Sleeping for {DELAY_BETWEEN_BATCHES} seconds before next batch.")
                        await asyncio.sleep(DELAY_BETWEEN_BATCHES)
                    total_processed += total_messages
                    logger.info(f"Processed {total_processed} messages so far.")

                except (errors.ServerError, errors.FloodWaitError, asyncio.TimeoutError) as e:
                    logger.error(f"Error occurred: {e}. Retrying after {RETRY_DELAY} seconds...")
                    time.sleep(RETRY_DELAY)
                    
            logger.info("Backup completed!")


if __name__ == "__main__":
    try:
        tg_chat = TelegramChatBackup()
        asyncio.run(tg_chat.backup_chat())
    except Exception as e:
        logger.error(f"Error occured: {e}", exc_info=True)


