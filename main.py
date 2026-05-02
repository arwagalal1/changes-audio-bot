import os
import io
import logging
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from mutagen.id3 import ID3, TIT2, TPE1, COMM, APIC, ID3NoHeaderError

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Helper: check admin
# ─────────────────────────────────────────────
async def is_admin(bot, user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False


# ─────────────────────────────────────────────
#  1. Audio received → show buttons
# ─────────────────────────────────────────────
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    audio = msg.audio or msg.voice
    if not audio:
        return

    msg_key = f"{msg.chat_id}_{msg.message_id}"

    # Save audio info in memory
    context.bot_data[msg_key] = {
        'file_id':   audio.file_id,
        'chat_id':   msg.chat_id,
        'file_name': getattr(audio, 'file_name', None) or 'audio.mp3',
        'performer': getattr(audio, 'performer', '') or '',
        'title':     getattr(audio, 'title', '')     or '',
    }

    keyboard = [
        [
            InlineKeyboardButton("🎤 تغيير اسم الفنان",    callback_data=f"artist|{msg_key}"),
            InlineKeyboardButton("📝 تغيير اسم الملف",     callback_data=f"filename|{msg_key}"),
        ],
        [
            InlineKeyboardButton("🖼️ تغيير الصورة المصغرة", callback_data=f"thumbnail|{msg_key}"),
            InlineKeyboardButton("📄 تغيير وصف الملف",     callback_data=f"description|{msg_key}"),
        ],
    ]
    await msg.reply_text(
        "🎵 اختر ما تريد تعديله:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────────
#  2. Button clicked → ask for new value
# ─────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Admin check
    if not await is_admin(context.bot, user_id, chat_id):
        await query.answer("❌ هذه الأزرار للأدمنز فقط!", show_alert=True)
        return

    await query.answer()

    action, msg_key = query.data.split('|', 1)
    audio_data = context.bot_data.get(msg_key)

    if not audio_data:
        await query.message.reply_text("⚠️ انتهت صلاحية هذا الملف. أرسله مرة أخرى.")
        return

    # Store editing state for this admin
    context.user_data['editing'] = {
        'action':     action,
        'audio_data': dict(audio_data),
        'chat_id':    chat_id,
        'admin_id':   user_id,
    }

    prompts = {
        'artist':      "✏️ أرسل اسم الفنان الجديد:",
        'filename':    "✏️ أرسل اسم الملف الجديد (بدون امتداد):",
        'thumbnail':   "🖼️ أرسل الصورة المصغرة الجديدة (صورة):",
        'description': "✏️ أرسل الوصف الجديد للملف:",
    }
    await query.message.reply_text(prompts[action])


# ─────────────────────────────────────────────
#  3. Admin sends new value → process & send
# ─────────────────────────────────────────────
async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    editing = context.user_data.get('editing')
    if not editing:
        return

    action = editing['action']
    msg    = update.message

    # Validate input type
    if action == 'thumbnail' and not msg.photo:
        return
    if action != 'thumbnail' and not msg.text:
        return

    status_msg = await msg.reply_text("⏳ جاري المعالجة، انتظر قليلاً...")

    try:
        audio_data = editing['audio_data']

        # ── Download original audio ──
        tg_file    = await context.bot.get_file(audio_data['file_id'])
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        # ── Write to temp file ──
        tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        tmp.write(audio_bytes)
        tmp.close()
        tmp_path = tmp.name

        # ── Load ID3 tags ──
        try:
            tags = ID3(tmp_path)
        except ID3NoHeaderError:
            tags = ID3()

        performer  = audio_data['performer']
        title      = audio_data['title']
        file_name  = audio_data['file_name']
        thumb_bytes = None

        # ── Apply edit ──
        if action == 'artist':
            performer = msg.text.strip()
            tags.delall('TPE1')
            tags.add(TPE1(encoding=3, text=performer))

        elif action == 'filename':
            new_name  = msg.text.strip()
            file_name = new_name if new_name.endswith('.mp3') else new_name + '.mp3'
            tags.delall('TIT2')
            tags.add(TIT2(encoding=3, text=new_name))

        elif action == 'description':
            tags.delall('COMM')
            tags.add(COMM(encoding=3, lang='ara', desc='', text=msg.text.strip()))

        elif action == 'thumbnail':
            photo_file  = await context.bot.get_file(msg.photo[-1].file_id)
            thumb_bytes = bytes(await photo_file.download_as_bytearray())
            tags.delall('APIC')
            tags.add(APIC(
                encoding=3, mime='image/jpeg',
                type=3, desc='Cover', data=thumb_bytes
            ))

        # ── Save & read final file ──
        tags.save(tmp_path)
        with open(tmp_path, 'rb') as f:
            final_audio = f.read()
        os.unlink(tmp_path)

        def get_thumb():
            return io.BytesIO(thumb_bytes) if thumb_bytes else None

        base_kwargs = dict(
            filename=file_name,
            performer=performer,
            title=title,
        )

        # ── Send to group ──
        await context.bot.send_audio(
            chat_id=editing['chat_id'],
            audio=io.BytesIO(final_audio),
            thumbnail=get_thumb(),
            caption="✅ تم التعديل بنجاح",
            **base_kwargs,
        )

        # ── Send to admin privately ──
        try:
            await context.bot.send_audio(
                chat_id=editing['admin_id'],
                audio=io.BytesIO(final_audio),
                thumbnail=get_thumb(),
                caption="✅ نسختك الشخصية من الملف المعدّل",
                **base_kwargs,
            )
        except Exception as e:
            logger.warning(f"Could not DM admin: {e}")

        await status_msg.edit_text("✅ تم! الملف المعدّل أُرسل للمجموعة وإليك شخصياً.")
        del context.user_data['editing']

    except Exception as e:
        logger.error(f"Processing error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ حدث خطأ أثناء المعالجة:\n{e}")


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
def main():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        raise SystemExit("❌ BOT_TOKEN environment variable is missing!")

    app = Application.builder().token(token).build()

    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
        handle_reply
    ))

    logger.info("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
