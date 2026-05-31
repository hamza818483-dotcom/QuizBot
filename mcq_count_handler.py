"""Custom MCQ Count Wrapper - Safe"""
from core_handlers import img_handler as original_img, txt_handler as original_txt

async def img_handler(update, context):
    """Enhanced /img with custom count"""
    args = context.args
    # If custom count given, inject into context
    if args and args[0].isdigit():
        context.user_data['mcq_count'] = int(args[0])
        context.user_data['mcq_topic'] = ' '.join(args[1:])
    else:
        context.user_data['mcq_count'] = 25
        context.user_data['mcq_topic'] = ''
    await original_img(update, context)

async def txt_handler(update, context):
    """Enhanced /txt with custom count"""
    args = context.args
    if args and args[0].isdigit():
        context.user_data['mcq_count'] = int(args[0])
        context.user_data['mcq_topic'] = ' '.join(args[1:])
    else:
        context.user_data['mcq_count'] = 25
        context.user_data['mcq_topic'] = ''
    await original_txt(update, context)
