"""Unambiguous local stop controls; all other language needs semantic planning."""
import re


PRIVACY_STOP_PHRASES = (
    "别听了", "不要听了", "不要再听了", "停止全部监听", "停止所有监听",
    "关闭麦克风", "关掉麦克风", "把麦克风关掉", "别再监听了", "停止监听", "停止录音", "关闭录音",
    "暂停摄像头和麦克风", "暂停麦克风和摄像头", "关闭摄像头和麦克风",
    "关闭麦克风和摄像头", "停止摄像头和麦克风", "停止麦克风和摄像头",
    "摄像头和麦克风都关掉", "麦克风和摄像头都关掉", "隐私模式", "隐私暂停",
    "stop listening", "stop recording", "privacy mode",
)
CAMERA_STOP_PHRASES = ("关闭摄像头", "暂停摄像头", "关闭观察", "别看了", "不要看了", "停止观察")
SPEECH_STOP_PHRASES = (
    "停", "停止", "别说了", "不要说了", "别讲了", "不要讲了", "别再讲", "不要再讲",
    "不用讲了", "先别讲", "停止讲解", "结束讲解", "别讲这道题", "不要讲这道题",
    "别再讲这道题", "不要继续讲这道题", "这题别讲了", "停止测试", "结束测试",
    "测试到这里", "停止这次测试", "停止说话", "安静一下", "stop speaking",
)


def is_direct_voice_control(text: str, phrases: tuple) -> bool:
    # Clause boundaries matter: removing all punctuation would turn a question
    # about a command, or a negated command, into a real device instruction.
    choices = "|".join(re.escape(re.sub(r"\s+", "", item)) for item in phrases)
    pattern = re.compile(
        r"(?:(?:请你|请|麻烦你|麻烦|帮我|现在|先|马上|立即|我们|咱们)){0,3}(?:"
        + choices + r")(?:(?:吧|了|一下|谢谢你|谢谢)){0,3}", re.IGNORECASE,
    )
    return any(pattern.fullmatch(re.sub(r"\s+", "", clause))
               for clause in re.split(r"[，。！,!.；;\n]", text))
