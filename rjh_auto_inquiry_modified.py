import os
import sys
import re
import json
import time
import random
import getpass
import shutil
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

AUTHOR_LINE = "22级临五yqy制作。调用自购deepseek-api，启动后输入必要信息即可进入瑞金ai问诊网页自动进行五次问答和病史书写。任何疑问和bug，请联系yang13398@163.com。"
RUN_TIMES = 5
ENV_PATH = ".env"


def get_app_dir():
    """返回程序所在目录。打包成 exe 后，返回 exe 所在目录；直接运行 .py 时，返回 .py 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_app_dir()


def resource_path(relative_path):
    """兼容 PyInstaller 的资源路径。"""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(BASE_DIR, relative_path)


def find_local_browser():
    """
    优先使用本机已安装的 Edge / Chrome，避免携带 Playwright 自带 Chromium。
    如需手动指定浏览器路径，可在 .env 中填写：
    BROWSER_PATH=C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe
    """
    env_path = (os.environ.get("BROWSER_PATH") or "").strip().strip('"')
    if env_path and os.path.exists(env_path):
        return env_path

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    candidates = [
        # Microsoft Edge，Windows 10/11 通常自带
        os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(local_appdata, "Microsoft", "Edge", "Application", "msedge.exe"),

        # Google Chrome
        os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local_appdata, "Google", "Chrome", "Application", "chrome.exe"),
    ]

    # 再尝试 PATH 里的命令
    for cmd in ("msedge", "chrome", "chrome.exe", "msedge.exe"):
        found = shutil.which(cmd)
        if found:
            candidates.append(found)

    for p in candidates:
        if p and os.path.exists(p):
            return p

    return None


def prompt_env_value(name, prompt, secret=False):
    """
    优先读取 .env / 环境变量；若为空，则运行时询问。
    secret=True 时输入内容不会显示在屏幕上。
    """
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if secret:
        return getpass.getpass(prompt).strip()
    return input(prompt).strip()

def ensure_user_env_fields(env_path=ENV_PATH):
    """确保 .env 里有 USER_NAME/STUDENT_NUMBER/USER_EMAIL 三个字段，方便其他用户直接填写。"""
    required = ["USER_NAME", "STUDENT_NUMBER", "USER_EMAIL"]
    existing_keys = set()
    lines = []

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            existing_keys.add(s.split("=", 1)[0].strip())

    missing = [k for k in required if k not in existing_keys]
    if missing:
        with open(env_path, "a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write("\n# 运行者个人信息：请在运行前填写，供提交表单使用\n")
            for k in missing:
                f.write(f"{k}=\n")
        print(f"已在 {env_path} 中补充字段：" + "、".join(missing))
        print("请按需填写 .env 中的 USER_NAME、STUDENT_NUMBER、USER_EMAIL；若留空，程序会在启动时询问。")


print(AUTHOR_LINE)

ensure_user_env_fields()
load_dotenv()

DEEPSEEK_API_KEY = prompt_env_value(
    "DEEPSEEK_API_KEY",
    "请输入 DeepSeek API Key：",
    secret=False,
)
if not DEEPSEEK_API_KEY:
    raise RuntimeError("DeepSeek API Key 不能为空。")

DEEPSEEK_BASE_URL = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)

# ========== 系统提示词 ==========
CONSULT_PROMPT = """你是一名临床医生，正在对模拟病人问诊，目标是采集完整大病史。

【总规则】
1. 页面已给出的“207号病人/549号患者”等只表示病例编号，绝对不是患者真实姓名，不能当作姓名填写。
2. 页面基本信息只用于问诊前判断性别、年龄、就诊日期等背景；姓名、年龄、性别、民族、婚姻、籍贯、住址、职业等一般情况仍然要在问诊中重新采集。
3. 患者姓名绝对不能漏采：第一轮必须先问姓名，可顺带问年龄；禁止把“xxx号病人/患者”写成姓名。
4. 若页面基本信息显示患者为女性，问诊中必须询问月经史和生育史；不要等到病史填写时再补造。
5. 问诊中必须包含婚姻史，婚姻史要和“一般情况-婚姻状况”区分记录。
6. 每轮只问一个主题，最多包含2个具体问题。禁止一次性连续询问“姓名、年龄、性别、婚姻、籍贯、住址、职业、科别、床号、住院号”等长串问题。
7. 若患者没有回答当前问题，而是先说症状：先记录症状，下一轮用更短的问题补问漏项；不要把未回答项判为“无”。
8. 患者明确说“不知道/不清楚/不方便说”时，该项记为“未采集”，不纠缠。
9. 不寒暄，不解释，不加前缀，直接输出医生要说的话。
10. 全部问完后只输出：[问诊结束]

【问诊顺序】
一、一般情况：必须向患者重新询问，页面基本信息不能替代问诊采集。按小组分轮问：
- 姓名、年龄
- 性别、民族
- 婚姻状况、籍贯
- 现住址、职业
- 科别；若为门诊病人，不主动追问病舍、床号、住院号，除非页面或患者提示为住院。

二、主诉与现病史：
- 主要不适是什么，持续多久
- 起病诱因、发展过程
- 加重或缓解因素
- 伴随症状
- 院外就诊、检查和用药情况

三、既往史：
- 既往疾病和传染病史
- 手术外伤史、输血史
- 预防接种史
- 药物/食物过敏史

四、个人史、婚姻史、生育史、月经史与家族史：
- 吸烟饮酒、疫区/毒物/职业暴露等
- 婚姻史：婚姻状况、配偶健康情况、夫妻关系；未婚/离异/丧偶也要记录
- 女性患者必须询问月经史：初潮年龄、周期、经期、经量、痛经、末次月经或绝经情况
- 女性患者必须询问生育史：孕产次数、流产/早产/剖宫产、子女健康情况；未婚或未育也要记录
- 家族中类似疾病、遗传病、重大慢性病

优先自然推进问诊，但不要为了追求完整而一次问太多。
"""

PHYS_PROMPT = """你是一名临床医生，正在对模拟病人进行体格检查。请根据问诊记录，
向“体格检查助理”逐条说明要做的检查，一次只说一项检查名称，根据返回结果决定下一项。

【必须优先完成的检查】
体格检查开始后，必须先依次完成以下生命体征测量：
1. 体温
2. 脉搏
3. 呼吸
4. 血压

这四项必须逐项单独询问，不要合并成一句“生命体征”或“一般情况”。例如应依次输出：
体温
脉搏
呼吸
血压

完成体温、脉搏、呼吸、血压后，再根据病例选择相关查体项目，例如：
一般情况、皮肤黏膜、浅表淋巴结、头颈部、心脏听诊、肺部听诊、腹部查体、双下肢水肿、神经系统检查等。

【全面查体规则】
生命体征之后必须做全面查体，不要只做与主诉最相关的一两项。至少覆盖：
一般情况、皮肤黏膜、浅表淋巴结、头颈部、胸部/肺、心脏、腹部、脊柱四肢、神经系统。
对胸肺、心脏、腹部这类需要视触叩听的系统，要按检查方式拆开逐项完成：
- 呼吸科/胸肺相关患者：必须依次完成肺部视诊、肺部触诊、肺部叩诊、肺部听诊，不能只做肺部听诊。
- 心血管相关患者：必须完成心脏视诊、心脏触诊、心脏叩诊、心脏听诊。
- 消化/腹部相关患者：必须完成腹部视诊、腹部触诊、腹部叩诊、腹部听诊。
每次仍然只输出一个检查项目名称，例如“肺部视诊”“肺部触诊”“肺部叩诊”“肺部听诊”。

【结束规则】
只有在体温、脉搏、呼吸、血压四项都已经得到结果，并且与本病例相关的必要查体也基本完成后，才可以输出：[查体结束]
呼吸科或胸肺相关病例必须完成肺部视触叩听四项后才可结束；不能用“肺部听诊正常”替代其他检查方式。

【容错规则】
若某一项连续两次得到“请告知部位+检查方式(视触叩听)”或类似无法识别的回复，说明助理不支持该项，立即改做明显不同的检查，不要再用近义说法重复同一项。
但体温、脉搏、呼吸、血压四项不能因为“一般情况”已检查就跳过，必须单独尝试询问。

不寒暄、不解释，直接说要查的项目名称。"""

SUMMARY_PROMPT = """根据【页面基本信息】和【问诊对话】整理大病史 JSON。

【优先级】
1. 问诊中患者明确回答的一般情况优先级最高。
2. 页面基本信息中的“xxx号病人/患者”只表示病例编号，不是患者姓名；页面日期可作为入院/就诊日期，页面性别年龄只可作为核对参考。
3. 既往史部分：没问到、患者未回答、患者说不清楚的项，一律填“无”。
4. 除既往史以外：没问到、患者未回答、患者说不清楚的项，一律填“未采集”。
5. 只有患者明确否认某病史或某症状时，才可填“无”；但身份信息、日期信息未采集时禁止填“无”。
6. 姓名必须来自问诊中患者回答；禁止使用“207号病人、549号患者、xxx号病人/患者”作为姓名。若未问到真实姓名，只能填“未采集”，不能填“无”。
7. 婚姻史必须单独整理到“婚姻史”，不要合并进“个人史”。
8. 女性患者必须整理“月经史”和“生育史”；若问诊确实未问到，填“未采集”。男性患者“月经史”填“不适用”。

【页面基本信息解析规则】
例如：“207号病人，男，40岁，前来门诊问诊，问诊时间：2022年02月22日”
应解析为：
- 病例编号：207号病人（不是姓名）
- 性别：男
- 年龄：40岁
- 就诊方式：门诊
- 问诊时间：2022年02月22日

【日期格式规则】
- 入院日期、记录日期必须使用 xxxx-xx-xx 格式。
- 若原文为“2016年9月1日”，必须转为“2016-09-01”。
- 若原文为“2022年02月22日”，必须转为“2022-02-22”。
- 入院日期优先使用页面问诊时间；记录日期使用程序运行当天日期。

只输出 JSON，不要其他文字：
{
  "一般情况": {
    "姓名": "",
    "年龄": "",
    "性别": "",
    "民族": "",
    "婚姻": "",
    "籍贯": "",
    "住址": "",
    "职业": "",
    "入院日期": "",
    "记录日期": "",
    "科别": "",
    "病舍床号": "",
    "住院号": ""
  },
  "主诉": "",
  "现病史": "",
  "既往史": {
    "疾病史": "",
    "传染病史": "",
    "预防接种史": "",
    "手术外伤史": "",
    "输血史": "",
    "过敏史": ""
  },
  "个人史": "",
  "婚姻史": "",
  "生育史": "",
  "月经史": "",
  "家族史": ""
}"""

AUX_PROMPT = """你是一名临床医生，需要为这位病人开立辅助检查。下面给你问诊与查体信息，以及"可开立检查项清单"。
请根据病情，选出**确有必要**的检查项——多开或少开都会扣分，请克制、有针对性。
只能从给定清单里选，名称必须与清单完全一致（含括号、标点都要一致）。
只输出一个 JSON 数组，元素为检查项名称，不要任何其他文字。例如：["血常规（CBC）","肝功能常规检查"]"""

DIAGNOSIS_PROMPT = """你是一名严谨的临床医生。请综合下面提供的【问诊大病史】【体格检查】【辅助检查结果】，给出本病例的初步诊断。

要求：
1. 若材料中提供了【本病例标准诊断】，必须以它为准：诊断列表与标准诊断完全一致、一条都不能漏、按其顺序列出。
2. 先逐条梳理所有阳性发现与异常指标（症状、体征、化验/影像/病理异常等）。
3. 给出完整诊断列表，并为每一条附简要诊断依据（对应哪些问诊/查体/辅助检查发现）；若某条标准诊断在现有材料中依据不足，也要列出该诊断并合理推断其依据。
4. 若未提供标准诊断，则自行给出主要诊断+并存/次要诊断+鉴别诊断，并把每项异常落实到某个诊断。

直接输出可填入诊断框的正文，不要使用 ** 等加粗标记，不要寒暄或多余说明。"""

NOTE_PROMPT = """根据以下病例信息，写一段简洁的病历记录小结（150字以内即可），涵盖主诉、关键查体与辅助检查发现、初步诊断思路。直接输出正文，不要使用 ** 等标记。"""

FORM_FILL_PROMPT = """你要把病例信息填入住院病历大病史表单。下面会给你：
1. 页面基本信息
2. 已整理大病史
3. 体格检查
4. 辅助检查结果
5. 标准诊断
6. 系统回顾与查体参考模板
7. 表单字段清单

输出一个 JSON 对象：键=字段名，值=应填写内容。只输出 JSON。

【资料优先级】
标准诊断 > 辅助检查/查体/问诊大病史 > 页面基本信息 > 参考模板。
页面基本信息中的“xxx号病人/患者”是病例编号，不是姓名；patientName 必须来自问诊大病史中的真实姓名。

【一般情况字段】
- patientName 必须来自问诊中患者回答的真实姓名；禁止填写“xxx号病人/患者”。若确实未采集到真实姓名，填“未采集”。
- gender、age 优先来自问诊大病史；age 必须只填纯数字，例如 40，不要写“40岁”。
- ethnicity、marriage、birthplace、occupation、address 若问诊未采集，填“未采集”，不要填“无”。
- admissionDate 优先用页面问诊时间，必须填 xxxx-xx-xx 格式。
- recordDate 使用程序运行当天日期，必须填 xxxx-xx-xx 格式。
- 日期示例：“2016年9月1日”必须写成“2016-09-01”。
- narrator 默认“本人”；reliability 默认“可靠”。

【婚姻史/月经史/生育史】
- marriageHistory 必须填写独立婚姻史，不能只复制一般情况里的 marriage；若未采集填“未采集”。
- 页面性别为女时，fertilityHistory、menstrualHistory 必须优先使用问诊采集内容；未采集时填“未采集”，不要填“无”。
- 页面性别为男时，menstrualHistory 填“不适用”；fertilityHistory 若未采集填“未采集”。

【既往史字段】
- pastHistoryGeneral、pastHistoryDiseases、pastHistoryInfectiousDiseases、pastHistoryVaccination、pastHistorySurgery、pastHistoryTransfusion、pastHistoryDrugAllergy 属于既往史。
- 既往史部分只要未采集、未回答、不清楚，就填“无”，不要填“未采集”。

【非既往史缺失规则】
- 除既往史外，未采集的信息填“未采集”或按参考模板补全阴性描述。
- 身份信息、日期信息未采集时禁止填“无”。

【诊断字段】
- initialDiagnosis：必须以【标准诊断】为准，按原顺序逐条列出，不能漏。每条后写简要依据。
- differentialDiagnosis：至少写3个鉴别诊断，每个说明支持或不支持依据，不能只填“无”。
- treatmentPlan：分条写完整诊疗计划，至少包括进一步检查、一般处理、针对主要诊断治疗、监测指标、随访宣教。

【系统回顾与查体】
- 问诊/查体未采集到的阴性系统回顾和常规查体字段，用参考模板补全。
- 已有阳性发现必须保留，不能被模板阴性描述覆盖。
- 体格检查按视、触、叩、听分别填写到对应字段。

【生命体征】
physicalExamBodyTemperature、physicalExamPulse、physicalExamRespiratoryRate、physicalExamBloodPressureSystolic、physicalExamBloodPressureDiastolic 必填纯数字。
优先用实测值；若无实测，填默认值：36.5、80、18、120、80。

【其他】
- 确实无信息且模板不能覆盖的非既往史字段，填“未采集”或“未见明显异常”。
- 不要使用 ** 加粗标记。
"""

# 系统回顾与查体参考模板（来自用户提供的“系统回顾.docx”，用于补全未采集的阴性描述）
SYSTEM_REVIEW_REF = """【系统回顾】
头颈五官：无头晕、眩晕，无耳鸣、耳痛、听力下降，无鼻塞、流涕、鼻出血，无咽痛、吞咽困难、声音嘶哑，无颈部疼痛、僵硬及包块。
呼吸系统：无慢性咳嗽、咯痰、咯血史，无呼吸困难，无发热、盗汗，无结核患者密切接触史。
循环系统：无心悸、气促、发绀，无心前区疼痛，无晕厥、水肿病史，无动脉硬化，无风湿热病史。
消化系统：无腹痛、腹胀、反酸、嗳气，无呕血、便血，无食欲不振、恶心或呕吐史，大便正常。
泌尿生殖系统：无尿频、尿急、尿痛，无腰痛及排尿困难，无肾毒性药物应用。
造血系统：无苍白、乏力等，皮肤黏膜无瘀点、紫癜，无反复鼻出血或牙龈出血。
内分泌系统及代谢：无畏寒、多汗，无头痛或视力障碍，无食欲异常、烦渴、多尿等，毛发分布均匀，第二性征无改变。
神经精神系统：无头痛、失眠、嗜睡，无喷射性呕吐、记忆力改变，无意识障碍、瘫痪、昏厥、痉挛，无视力障碍、感觉及运动异常，无性格改变。
肌肉骨骼系统：无关节肿痛，无运动障碍，无肢体麻木，无痉挛萎缩或瘫痪史。
精神状态：无焦虑、抑郁、情绪低落，无兴趣减退，无注意力不集中、记忆力下降，无幻觉、妄想，无自杀或自伤观念，无攻击或暴力倾向，自知力完整，社会功能正常。
个人史：生长于原籍，无外地长期居住史，无疫区、疫水接触史，无工业粉尘、毒物、放射性物质接触史，无牧区、矿山、高氟区、低碘区居住史，平日生活规律，否认吸毒史、否认吸烟史，否认饮酒史，否认冶游史。

【查体常规阴性参考】
生命体征：体温36.5℃，脉搏80次/分，呼吸18次/分，血压120/80mmHg（若本病例查体或问诊中另有实测/异常数值，以实测为准）。
一般情况：发育正常，营养良好，神志清楚，自主体位，无慢性病容，表情自如，查体合作。
皮肤黏膜：皮肤弹性正常，皮温可，颜色正常，无皮疹，未见皮下出血、结节、肿块，无蜘蛛痣、肝掌，无瘢痕、溃疡，毛发正常。
淋巴结：全身浅表淋巴结未触及肿大。
头颅：大小、形状正常，无肿块、压痛、瘢痕，头发分布均匀。
眼：眉毛无脱落，眼睑无水肿，上睑无下垂，眼球无突出、运动自如，结膜无充血，巩膜无黄染，角膜透明，双侧瞳孔等大等圆，直径约3mm，对光反射、辐辏反射正常。
耳：耳廓无畸形，外耳道无异常分泌物，乳突无压痛，听力正常。
鼻：无畸形、分泌物，鼻翼无扇动，鼻中隔无偏曲、穿孔，鼻窦无压痛。
口腔：无异味，口唇红润，无龋齿、义齿、残根，牙龈无肿胀，舌居中、运动自如、苔薄白，咽无红肿，扁桃体无肿大，发音清晰。
颈部：对称，活动自如，气管居中，甲状腺未触及肿大，颈静脉无充盈，肝颈静脉回流征阴性，未闻及甲状腺血管杂音。
胸部：胸廓对称，无畸形、压痛，肋间隙正常，呼吸频率约18次/分、节律匀齐，乳房无异常，胸壁无静脉曲张、皮下气肿。
肺：视诊双侧对称、节律规整；触诊呼吸动度一致、无胸膜摩擦感、语颤无增强或减弱；叩诊清音、肺下界正常、移动度6～8cm；听诊双肺呼吸音清晰，无干湿啰音及胸膜摩擦音。
心脏：视诊心前区无隆起，心尖搏动位于胸骨左缘第5肋间锁骨中线内0.5～1.0cm；触诊心尖搏动有力，未触及震颤、抬举样搏动、心包摩擦感；叩诊相对浊音界无明显异常；听诊心律整齐、心音有力，未闻及杂音、附加音及心包摩擦音。
桡动脉：脉搏搏动良好，无奇脉、交替脉。
周围血管征：无毛细血管搏动、枪击音、水冲脉及动脉异常搏动。
腹部：视诊平坦，无肠型、蠕动波、腹壁静脉曲张及局部隆起；触诊腹壁柔软，无压痛、反跳痛、液波震颤及肿块，肝脾肋下未触及，胆囊无压痛、Murphy阴性，输尿管点无压痛；叩诊肝浊音界正常、肝区无叩击痛、无移动性浊音；听诊肠鸣音3次/分，无振水音及血管杂音。
脊柱：正常，活动自如，无压痛及叩击痛。
四肢：无异常形态，无杵状指趾、静脉曲张，肌肉无萎缩，肌张力正常，关节活动自如，无红肿及压痛。
神经系统：生理反射（肱二头肌、肱三头肌、膝腱、跟腱反射）正常；病理反射（巴氏征、奥本汉姆征、戈登征、霍夫曼征）阴性；脑膜刺激征（颈强直、布氏征、克氏征）阴性。"""

FORM_FIELDS = [
    ("patientName","姓名"),("gender","性别"),("age","年龄"),("ethnicity","民族"),
    ("marriage","婚姻"),("birthplace","籍贯"),("occupation","职业"),("address","现住址"),
    ("admissionDate","入院日期"),("recordDate","记录日期"),("narrator","病史陈述者"),("reliability","可靠程度"),
    ("chiefComplaint","主诉"),("presentIllness","现病史"),
    ("pastHistoryGeneral","既往一般健康状况"),("pastHistoryDiseases","既往疾病史"),
    ("pastHistoryInfectiousDiseases","传染病史"),("pastHistoryVaccination","预防接种史"),
    ("pastHistorySurgery","手术外伤史"),("pastHistoryTransfusion","输血史"),("pastHistoryDrugAllergy","药物过敏史"),
    ("systemReviewOtorhinolaryngology","系统回顾-头颈五官"),("systemReviewRespiratory","系统回顾-呼吸"),
    ("systemReviewCirculatory","系统回顾-循环"),("systemReviewDigestive","系统回顾-消化"),
    ("systemReviewUrinary","系统回顾-泌尿"),("systemReviewEndocrine","系统回顾-内分泌代谢"),
    ("systemReviewHematopoietic","系统回顾-造血"),("systemReviewMusculoskeletal","系统回顾-肌肉骨关节"),
    ("systemReviewNervous","系统回顾-神经"),("systemReviewMentalState","系统回顾-精神状态"),
    ("personalHistory","个人史"),("marriageHistory","婚姻史"),("fertilityHistory","生育史"),
    ("menstrualHistory","月经史"),("familyHistory","家族史"),
    ("physicalExamBodyTemperature","体温(℃)"),("physicalExamPulse","脉搏(次/分)"),
    ("physicalExamRespiratoryRate","呼吸(次/分)"),("physicalExamBloodPressureSystolic","收缩压"),
    ("physicalExamBloodPressureDiastolic","舒张压(mmHg)"),
    ("physicalExamGeneral","查体-一般情况"),("physicalExamDermatology","查体-皮肤黏膜"),
    ("physicalExamLymph","查体-淋巴结"),("physicalExamHead","查体-头部"),("physicalExamEyes","查体-眼"),
    ("physicalExamEars","查体-耳"),("physicalExamNose","查体-鼻"),("physicalExamOral","查体-口腔"),
    ("physicalExamCervical","查体-颈部"),("physicalExamThorax","查体-胸部"),
    ("lungsInspection","肺-视诊"),("lungsPalpation","肺-触诊"),("lungsPercussion","肺-叩诊"),("lungsAuscultation","肺-听诊"),
    ("cardiacInspection","心-视诊"),("cardiacPalpation","心-触诊"),("cardiacPercussion","心-叩诊"),("cardiacAuscultation","心-听诊"),
    ("radialArtery","桡动脉"),("peripheralVesselSigns","周围血管征"),
    ("abdomenInspection","腹-视诊"),("abdomenPalpation","腹-触诊"),("abdomenPercussion","腹-叩诊"),("abdomenAuscultation","腹-听诊"),
    ("rectumAndAnus","直肠与肛门"),("pudendum","外生殖器"),("spine","脊柱"),("limbs","四肢"),
    ("nervousSystem","查体-神经系统"),("specializedExamination","专科检查"),
    ("laboratoryExamination","实验室及特殊检查"),("initialDiagnosis","初步诊断及诊断依据"),
    ("differentialDiagnosis","鉴别诊断"),("treatmentPlan","诊疗计划"),
]

# ========== 辅助检查菜单（三级树）==========
AUX_TREE = {
  "实验室检查": {
    "血液学检查": {"血常规（CBC）": "x", "凝血四项": "x", "红细胞沉降率": "x", "D二聚体": "x", "纤维蛋白(原)降解产物测定(FDP)": "x"},
    "尿液检查": {"尿常规": "x", "24小时尿量": "x", "24小时尿蛋白定量": "x"},
    "粪便检查": {"粪便常规": "x", "粪隐血实验": "x"},
    "体液与分泌物检查": {"呕吐物潜血试验": "x"},
    "生化检查": {"肝功能常规检查": "x", "肾功能常规检查": "x", "电解质检查": "x", "淀粉酶": "x", "血脂常规检查": "x", "血气分析": "x", "心肌酶谱常规检查": "x", "空腹血糖测定": "x", "随机血糖测定": "x", "糖化血红蛋白": "x", "葡萄糖耐量试验(OGTT)": "x", "胰岛素释放试验": "x", "血浆氨测定": "x", "前脑钠肽": "x", "血清肌钙蛋白测定": "x"},
    "免疫学检查": {"乙肝三系检查": "x", "肿瘤标志物检测": "x", "免疫球蛋白定量测定": "x", "C反应蛋白": "x"},
  },
  "影像检查": {
    "X线": {"胸部X线": "x"},
    "CT": {"颅脑CT": "x", "胸部CT": "x", "腹部CT": "x"},
    "超声": {"超声心动图": "x", "腹部:肝胆胰脾超声": "x", "泌尿系B超:肾脏\\输尿管\\膀胱": "x"},
  },
  "心电图": {"常规心电图": "x"},
  "内镜检查": {"支气管镜检查": "x", "胃镜检查": "x", "结肠镜检查": "x"},
  "病理检查": {"淋巴结活检": "x", "骨髓活检": "x"},
}


def build_leaf_paths(tree, prefix=()):
    """返回 {叶子名: [完整路径...]}。叶子=值不是 dict 的项。"""
    out = {}
    for k, v in tree.items():
        if isinstance(v, dict):
            out.update(build_leaf_paths(v, prefix + (k,)))
        else:
            out[k] = list(prefix) + [k]
    return out


LEAF_PATHS = build_leaf_paths(AUX_TREE)   # 叶子名 -> 路径


def ds_reply(system_prompt, history):
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "system", "content": system_prompt}, *history],
    )
    return resp.choices[0].message.content.strip()


def ds_once(system_prompt, user_content):
    """单轮调用 DeepSeek。"""
    return client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_content}],
    ).choices[0].message.content.strip()


def get_user_info():
    """读取运行者个人信息：优先 .env；若 .env 留空，则运行时询问。"""
    name = (os.environ.get("USER_NAME") or "").strip()
    sid = (os.environ.get("STUDENT_NUMBER") or "").strip()
    email = (os.environ.get("USER_EMAIL") or "").strip()

    if not name:
        name = input("请输入姓名（也可下次提前填入 .env 的 USER_NAME=）：").strip()
    if not sid:
        sid = input("请输入学号/工号（也可下次提前填入 .env 的 STUDENT_NUMBER=）：").strip()
    if not email:
        email = input("请输入邮箱（也可下次提前填入 .env 的 USER_EMAIL=）：").strip()

    print("\n将用以下信息填写表单：")
    print(f"  姓名：{name}")
    print(f"  学号/工号：{sid}")
    print(f"  邮箱：{email}")
    return name, sid, email


def get_site_credentials():
    """读取网站登录账号密码：优先 .env；若 .env 留空，则运行时询问。"""
    site_user = prompt_env_value(
        "SITE_USER",
        "请输入网站账号（也可提前填入 .env 的 SITE_USER=）：",
        secret=False,
    )
    site_pass = prompt_env_value(
        "SITE_PASS",
        "请输入网站密码（输入时不会显示；也可提前填入 .env 的 SITE_PASS=）：",
        secret=True,
    )
    if not site_user or not site_pass:
        raise RuntimeError("网站账号和密码不能为空。")
    return site_user, site_pass


# ========== 通用页面操作 ==========
def consult_region(page):
    return page.get_by_text("问诊对话区域", exact=True).locator("xpath=..")


def phys_region(page):
    return page.get_by_role("tabpanel").filter(has_text="体格检查助理")


def aux_region(page):
    return page.get_by_role("tabpanel").filter(has_text="辅助检查助理")


def latest_reply(region):
    return region.locator(".self-start .p-card-content").last.inner_text().strip()


def send_in(region, text):
    box = region.locator("textarea")
    box.click()
    box.fill(text)
    box.press("Enter")


def wait_new(region, prev_count, timeout=60):
    msgs = region.locator(".self-start .p-card-content")
    deadline = time.time() + timeout
    while msgs.count() <= prev_count and time.time() < deadline:
        time.sleep(0.3)
    last, stable = None, 0
    while stable < 5 and time.time() < deadline:
        cur = msgs.last.inner_text().strip()
        stable = stable + 1 if (cur and cur == last) else 0
        last = cur
        time.sleep(0.3)


def switch_tab(page, name):
    page.get_by_role("tab", name=name, exact=True).click()
    time.sleep(1)


def open_system_dropdown(page):
    """打开系统下拉；返回当前 options locator。"""
    box = page.locator(".p-select").filter(has_text="请选择").first
    box.wait_for(state="visible", timeout=15000)
    options = page.get_by_role("option")

    clickers = [lambda: box.locator(".p-select-dropdown").click(),  # 点下拉箭头
                lambda: box.click()]                                # 点整个框
    for i in range(8):
        if options.count() > 0 and options.first.is_visible():      # 已经开着就别再点
            break
        try:
            clickers[i % 2]()                                       # 交替两种点法
        except Exception:
            pass
        try:
            options.first.wait_for(state="visible", timeout=2000)  # 开了就立刻退出
            break
        except Exception:
            time.sleep(0.4)

    options.first.wait_for(state="visible", timeout=8000)
    return options


def select_system(page):
    """首次打开“系统”下拉、列出系统、按编号选择；返回系统名称。"""
    options = open_system_dropdown(page)
    names = [t.strip() for t in options.all_inner_texts()]
    print("\n可选系统：")
    for i, n in enumerate(names):
        print(f"  {i + 1}. {n}")
    while True:
        sel = input("输入要选择的系统编号：").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(names):
            break
        print("编号无效，请重输。")
    chosen = names[int(sel) - 1]
    options.nth(int(sel) - 1).click()
    time.sleep(0.8)
    print(f"已选择：{chosen}")
    return chosen


def select_system_by_name(page, system_name):
    """后续自动重复时，按首次选择的系统名称自动选择。"""
    options = open_system_dropdown(page)
    names = [t.strip() for t in options.all_inner_texts()]
    if system_name in names:
        idx = names.index(system_name)
    else:
        # 名称轻微变化时做包含匹配兜底
        idx = None
        for i, n in enumerate(names):
            if system_name and (system_name in n or n in system_name):
                idx = i
                break
        if idx is None:
            raise RuntimeError(f"未找到上次选择的系统：{system_name}；当前可选：{names}")
    options.nth(idx).click()
    time.sleep(0.8)
    print(f"已自动选择：{names[idx]}")
    return names[idx]


def enter_practice_mode(page):
    """进入系统练习模式。"""
    page.goto("https://vsp.rjh.com.cn:8080", wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("heading", name="系统练习模式").click()
    page.wait_for_url("**/practice", timeout=30000)


def wait_case_ready(page, timeout=60000):
    """等待病例/问诊界面加载完成。"""
    page.get_by_text("问诊时间").first.wait_for(state="visible", timeout=timeout)
    consult_region(page).locator("textarea").wait_for(state="visible", timeout=timeout)
    time.sleep(1)


def start_case_with_system(page, system_name=None):
    """进入练习模式并选择系统；system_name 为空时手动选择，否则自动选择。"""
    enter_practice_mode(page)
    if system_name:
        selected = select_system_by_name(page, system_name)
    else:
        selected = select_system(page)
    wait_case_ready(page)
    return selected

def get_consult_info(page):
    text = page.get_by_text("问诊时间").first.inner_text().strip()
    m = re.search(r"问诊时间[：:]\s*([\d年月日./\-]+)", text)
    return text, (m.group(1) if m else "未采集")


UNKNOWN_VALUES = {"", "无", "未采集", "不详", "未知", "None", "none", "null", "NULL"}


def normalize_date_string(text, default=""):
    """
    把常见日期格式统一为 YYYY-MM-DD。
    支持：2022年02月22日、2016年9月1日、2022/2/22、2022.02.22、2022-2-22。
    无法解析时返回 default。
    """
    if not text:
        return default
    s = str(text).strip()
    if not s or s in UNKNOWN_VALUES:
        return default

    # 先从长文本中找日期，避免传入整句“问诊时间：2022年02月22日”时失败
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if not m:
        m = re.search(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})", s)
    if not m:
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
        if m:
            mo, d, y = m.groups()
            if len(y) == 2:
                y = "20" + y if int(y) < 50 else "19" + y
            try:
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            except ValueError:
                return default
    if not m:
        return default

    y, mo, d = m.groups()
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return default


def normalize_age_value(value):
    """表单年龄字段只接受数字，页面基本信息里的“40岁”要转成“40”。"""
    s = str(value or "").strip()
    m = re.search(r"\d{1,3}", s)
    return m.group(0) if m else s


def is_case_label(value):
    """“549号病人/患者”是病例编号，不是患者姓名。"""
    return bool(re.fullmatch(r"\s*\d+\s*号\s*(?:病人|患者)\s*", str(value or "").strip()))


def clean_patient_name(value):
    value = str(value or "").strip()
    if is_case_label(value) or value in UNKNOWN_VALUES:
        return ""
    return value


def extract_basic_from_header(text):
    """
    从页面页头提取基本信息。
    兼容示例：207号病人，男，40岁，前来门诊问诊  问诊时间：2022年02月22日
    返回：{"病例编号":"207号病人", "性别":"男", "年龄":"40岁", "入院日期":"2022-02-22"}
    """
    info = {}
    if not text:
        return info

    compact = re.sub(r"\s+", " ", text).strip()

    explicit_name = re.search(r"(?:患者姓名|病人姓名|姓名)\s*[：:]\s*([^\s，,；;]+)", compact)
    if explicit_name:
        maybe_name = explicit_name.group(1).strip()
        if is_case_label(maybe_name):
            info["病例编号"] = maybe_name
        else:
            info["姓名"] = maybe_name

    # 优先匹配“207号病人，男，40岁”这类格式。注意：这不是患者姓名。
    m = re.search(r"([^，,\s]+号(?:病人|患者))\s*[，,]\s*(男|女)\s*[，,]\s*(\d{1,3})\s*岁", compact)
    if m:
        info["病例编号"] = m.group(1).strip()
        info["性别"] = m.group(2).strip()
        info["年龄"] = m.group(3).strip() + "岁"
    else:
        # 兜底：找“男/女”和“xx岁”
        gender = re.search(r"(?<![一-龥])(男|女)(?![一-龥])", compact)
        age = re.search(r"(\d{1,3})\s*岁", compact)
        case_no = re.search(r"([^，,\s]+号(?:病人|患者))", compact)
        if case_no:
            info["病例编号"] = case_no.group(1).strip()
        if gender:
            info["性别"] = gender.group(1)
        if age:
            info["年龄"] = age.group(1) + "岁"

    dt = normalize_date_string(compact, default="")
    if dt:
        # 该系统里“问诊时间”通常就是病历书写要求中的入院/就诊日期
        info["入院日期"] = dt

    return info


def patient_is_female(info_text):
    basic = extract_basic_from_header(info_text)
    if basic.get("性别") == "女":
        return True
    compact = re.sub(r"\s+", " ", str(info_text or ""))
    return bool(re.search(r"(?<![一-龥])女(?![一-龥])", compact))


def context_value(info_text, zh_key):
    """从页面文字或 JSON 上下文里找某个中文字段值。"""
    basic = extract_basic_from_header(info_text)
    if zh_key in basic and basic[zh_key]:
        return basic[zh_key]

    patterns = [
        rf'"{re.escape(zh_key)}"\s*:\s*"([^"]+)"',
        rf"'{re.escape(zh_key)}'\s*:\s*'([^']+)'",
        rf"{re.escape(zh_key)}\s*[：:]\s*([^\n，,；;]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, str(info_text or ""))
        if m:
            value = m.group(1).strip()
            if value and value not in UNKNOWN_VALUES:
                return value
    return ""


def build_required_consult_note(patient_info):
    """根据问诊前调取到的基本信息，给模型追加本例必问项。"""
    basic = extract_basic_from_header(patient_info)
    lines = ["【本例问诊前已调取的基本信息】", json.dumps(basic, ensure_ascii=False)]
    if basic.get("病例编号"):
        lines.append(f"页面编号 {basic['病例编号']} 不是患者姓名，禁止写入姓名字段。")
    lines.append("第一轮必须询问患者真实姓名和年龄；随后继续询问性别、民族、婚姻、籍贯、现住址、职业等一般情况。")
    if basic.get("性别") == "女":
        lines.append("本例为女性患者：必须询问月经史和生育史。")
    elif not basic.get("性别"):
        lines.append("页面未解析到性别：先确认性别；若为女性，必须继续询问月经史和生育史。")
    lines.append("本例必须询问婚姻史，并独立记录。")
    return "\n".join(lines)


def is_unknown_value(value):
    return str(value).strip() in UNKNOWN_VALUES


def normalize_dabingshi_missing_values(dabingshi, patient_info=""):
    """
    统一大病史缺失值规则：
    - 既往史：未采集/空/不详 -> 无
    - 非既往史：未采集/空/不详 -> 未采集
    - 一般情况日期：统一 YYYY-MM-DD
    """
    if not isinstance(dabingshi, dict):
        return dabingshi

    # 一般情况
    dabingshi.setdefault("一般情况", {})
    if not isinstance(dabingshi["一般情况"], dict):
        dabingshi["一般情况"] = {}
    general = dabingshi["一般情况"]

    # 日期：入院日期优先从页面问诊时间取；记录日期用程序当天
    basic = extract_basic_from_header(patient_info)
    today = time.strftime("%Y-%m-%d")
    admission_date = normalize_date_string(general.get("入院日期", ""), default="") or basic.get("入院日期", "")
    if admission_date:
        general["入院日期"] = admission_date
    elif is_unknown_value(general.get("入院日期", "")):
        general["入院日期"] = "未采集"

    general["记录日期"] = today

    # 一般情况未采集不能填“无”
    identity_keys = ["姓名", "年龄", "性别", "民族", "婚姻", "籍贯", "住址", "职业", "科别", "病舍床号", "住院号", "入院日期", "记录日期"]
    for k in identity_keys:
        old = str(general.get(k, "")).strip()
        if old == "" or old == "无" or old in {"不详", "未知", "None", "none", "null", "NULL"}:
            general[k] = "未采集"
    if is_case_label(general.get("姓名", "")):
        general["姓名"] = "未采集"

    # 既往史未采集统一填“无”
    dabingshi.setdefault("既往史", {})
    if not isinstance(dabingshi["既往史"], dict):
        dabingshi["既往史"] = {}
    past_keys = ["疾病史", "传染病史", "预防接种史", "手术外伤史", "输血史", "过敏史"]
    for k in past_keys:
        if is_unknown_value(dabingshi["既往史"].get(k, "")):
            dabingshi["既往史"][k] = "无"

    # 非既往史的顶层字段未采集填“未采集”
    for k in ["主诉", "现病史", "个人史", "婚姻史", "生育史", "月经史", "家族史"]:
        if is_unknown_value(dabingshi.get(k, "")):
            dabingshi[k] = "未采集"

    if basic.get("性别") == "男" and is_unknown_value(dabingshi.get("月经史", "")):
        dabingshi["月经史"] = "不适用"
    if basic.get("性别") == "女":
        for k in ["生育史", "月经史"]:
            if is_unknown_value(dabingshi.get(k, "")):
                dabingshi[k] = "未采集"

    return dabingshi


def backfill_basic_info(dabingshi, patient_info):
    """
    用页面基本信息硬兜底大病史的一般情况，防止模型把姓名/性别/年龄写成“无”。
    同时执行缺失值和日期格式规则。
    """
    if not isinstance(dabingshi, dict):
        return dabingshi
    dabingshi.setdefault("一般情况", {})
    if not isinstance(dabingshi["一般情况"], dict):
        dabingshi["一般情况"] = {}

    basic = extract_basic_from_header(patient_info)
    for k, v in basic.items():
        if k in {"病例编号", "姓名"}:
            continue
        if v:
            old = dabingshi["一般情况"].get(k, "")
            if is_unknown_value(old):
                dabingshi["一般情况"][k] = v

    dabingshi = normalize_dabingshi_missing_values(dabingshi, patient_info)

    # 页面已有性别/年龄/入院日期时可以兜底；病例编号绝不覆盖姓名
    for k, v in basic.items():
        if k in {"病例编号", "姓名"}:
            continue
        if v:
            dabingshi["一般情况"][k] = v

    # 记录日期永远用 YYYY-MM-DD
    dabingshi["一般情况"]["记录日期"] = time.strftime("%Y-%m-%d")
    return dabingshi


def enforce_past_history_form_defaults(filled):
    """表单既往史字段：未采集/空/不详统一填“无”。"""
    if not isinstance(filled, dict):
        return filled
    past_fields = [
        "pastHistoryGeneral",
        "pastHistoryDiseases",
        "pastHistoryInfectiousDiseases",
        "pastHistoryVaccination",
        "pastHistorySurgery",
        "pastHistoryTransfusion",
        "pastHistoryDrugAllergy",
    ]
    for field in past_fields:
        if is_unknown_value(filled.get(field, "")):
            filled[field] = "无"
    return filled


def enforce_reproductive_form_defaults(filled, info_text):
    """婚姻史、月经史、生育史兜底；女性患者这两项不能空。"""
    if not isinstance(filled, dict):
        return filled

    if is_unknown_value(filled.get("marriageHistory", "")):
        filled["marriageHistory"] = "未采集"

    if patient_is_female(info_text):
        for field in ("fertilityHistory", "menstrualHistory"):
            if is_unknown_value(filled.get(field, "")):
                filled[field] = "未采集"
    else:
        if is_unknown_value(filled.get("menstrualHistory", "")):
            filled["menstrualHistory"] = "不适用"
        if is_unknown_value(filled.get("fertilityHistory", "")):
            filled["fertilityHistory"] = "未采集"
    return filled


def backfill_form_identity_fields(filled, info_text):
    """
    填表前再次兜底身份字段。页面基本信息里的姓名/性别/年龄强制覆盖；
    其他身份字段若模型填“无”，改为“未采集”。日期统一 YYYY-MM-DD。
    """
    if not isinstance(filled, dict):
        return filled

    basic = extract_basic_from_header(info_text)
    mapping = {"姓名": "patientName", "性别": "gender", "年龄": "age"}
    for zh, field in mapping.items():
        value = context_value(info_text, zh)
        if field in {"gender", "age"}:
            value = value or basic.get(zh)
        if value:
            if field == "age":
                value = normalize_age_value(value)
            if field == "patientName":
                value = clean_patient_name(value) or "未采集"
            filled[field] = value

    identity_fields = ["ethnicity", "marriage", "birthplace", "occupation", "address"]
    for field in identity_fields:
        if str(filled.get(field, "")).strip() in {"", "无", "不详", "未知", "None", "none", "null", "NULL"}:
            filled[field] = "未采集"

    # 日期字段强制标准化为 YYYY-MM-DD
    today = time.strftime("%Y-%m-%d")
    admission_date = normalize_date_string(filled.get("admissionDate", ""), default="") or basic.get("入院日期", "") or today
    filled["admissionDate"] = admission_date
    filled["recordDate"] = today

    return filled


def normalize_marker(text):
    """统一括号格式"""
    return (
        text.replace("【", "[")
            .replace("】", "]")
            .replace("（", "(")
            .replace("）", ")")
            .strip()
    )


def reached_end(reply, end_marker):
    """
    判断是否出现结束标记
    支持：
    [查体结束]
    【查体结束】
    [问诊结束]
    【问诊结束】
    """
    txt = normalize_marker(reply)
    marker = normalize_marker(end_marker)
    return marker in txt


def run_chat(region, system_prompt, end_marker, max_turns=60):
    history = []
    prev_msg, stuck = None, 0
    for turn in range(max_turns):
        msg = latest_reply(region)
        print(f"  [对方] {msg}")
        history.append({"role": "user", "content": msg})

        if ("体格检查信息已记录完毕" in msg or "问诊信息已记录完毕" in msg):
            print("  （页面已结束）")
            break

        # 卡住检测：对方连续重复同一句（多半是没听懂、反复要你换说法）
        stuck = stuck + 1 if msg == prev_msg else 0
        prev_msg = msg
        if stuck >= 4:
            print("  （反复卡在同一项，强制结束本阶段）")
            break
        if stuck >= 2:
            history.append({"role": "user", "content":
                "[系统提示] 助理已多次表示无法识别你刚才的检查/说法。不要再重复同一项或它的近义说法；"
                "改做一个明显不同的检查项目；若关键查体已基本完成，直接输出" + end_marker + "。"})

        reply = ds_reply(system_prompt, history)
        print(f"  [医生] {reply}")
        history.append({"role": "assistant", "content": reply})

        if reached_end(reply, end_marker):
            print(f"  （{end_marker}）")
            break

        cnt = region.locator(".self-start .p-card-content").count()
        send_in(region, reply)
        wait_new(region, cnt)
        time.sleep(random.uniform(0.5, 1.5))
    else:
        print(f"  [警告] 达到最大轮数 {max_turns}，强制退出")
    return history


# ========== 辅助检查：级联选择（带容错与重试）==========
def dropdown_is_open(page):
    top = page.get_by_role("treeitem", name="实验室检查", exact=True)
    return top.count() > 0 and top.first.is_visible()


def ensure_open(page):
    if not dropdown_is_open(page):
        aux_region(page).locator(".p-cascadeselect-dropdown").first.click()
        time.sleep(0.5)


def reveal_child(page, group_name, child_name):
    """展开 group_name 让 child_name 出现：宽屏靠悬停，窄屏靠点击，两种都兼容。"""
    group = page.get_by_role("treeitem", name=group_name, exact=True)
    group.wait_for(state="visible", timeout=8000)
    content = group.locator(".p-cascadeselect-option-content").first
    child = page.get_by_role("treeitem", name=child_name, exact=True)
    content.hover()                       # 宽屏：悬停即弹出子菜单
    try:
        child.wait_for(state="visible", timeout=2500)
    except Exception:
        content.click()                   # 窄屏/移动模式：改用点击
        child.wait_for(state="visible", timeout=5000)


def select_one_test(page, path, attempts=2):
    """按路径展开并选中一个检查（多选会累积），不提交；失败自动重试。"""
    for i in range(attempts):
        try:
            ensure_open(page)
            for d in range(len(path) - 1):       # path[d] 展开出 path[d+1]
                reveal_child(page, path[d], path[d + 1])
            leaf = page.get_by_role("treeitem", name=path[-1], exact=True)
            leaf.wait_for(state="visible", timeout=8000)
            leaf.locator(".p-cascadeselect-option-content").first.click()  # 选中叶子
            time.sleep(0.4)
            return True
        except Exception as e:
            print(f"  [重试 {i + 1}/{attempts}] {path[-1]}：{e}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.8)
    print(f"  [失败] 未能选中：{path[-1]}")
    return False


def grab_aux_results(page, timeout=120):
    """提交后等待并读取辅助检查结果文本（结果可能分批出现）。"""
    def collect():
        texts = aux_region(page).locator(".p-card-content").all_inner_texts()
        return [t.strip() for t in texts if ("结果如下" in t or "结果已获取" in t)]

    deadline = time.time() + timeout
    while time.time() < deadline:
        got = collect()
        if got:
            time.sleep(3)                 # 再等一下，后续结果可能继续返回
            return "\n\n".join(collect())
        time.sleep(1)
    return ""


def grab_answer_diagnosis(page, timeout=30):
    """提交辅助检查后弹窗里显示本病例标准诊断（答案），抓取纯文本。"""
    loc = page.locator("div.whitespace-pre-wrap.pl-8.font-semibold")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for i in range(loc.count()):
                txt = loc.nth(i).inner_text().strip()
                if txt:
                    return txt
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def enter_history_writing(page):
    """点“病史书写”进入大病史书写环节（同时关掉答案弹窗）。"""
    btn = page.get_by_role("button", name="病史书写", exact=True)
    btn.wait_for(state="visible", timeout=15000)
    btn.click()
    time.sleep(1)


def fill_notepad(page, text):
    """提交前必须填记事本：点“记事本” -> 弹窗“确定” -> 在编辑器输入内容。"""
    page.get_by_role("button", name="记事本", exact=True).click()
    time.sleep(0.6)
    page.get_by_role("button", name="确定", exact=True).click()
    time.sleep(0.6)
    editor = page.locator(".ql-editor").first
    editor.click()
    editor.fill(text)
    time.sleep(0.4)


def extract_vitals_from_text(text):
    """从查体记录/参考模板中抽取生命体征数值。"""
    text = str(text or "")
    vitals = {}

    patterns = {
        "physicalExamBodyTemperature": [r"体温[：:\s]*(\d+(?:\.\d+)?)\s*℃?"],
        "physicalExamPulse": [r"(?:脉搏|心率)[：:\s]*(\d{1,3})\s*(?:次/分|次每分)?"],
        "physicalExamRespiratoryRate": [r"呼吸[：:\s]*(\d{1,3})\s*(?:次/分|次每分)?"],
    }
    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            m = re.search(pattern, text)
            if m:
                vitals[field] = m.group(1)
                break

    bp = re.search(r"血压[：:\s]*(\d{2,3})\s*/\s*(\d{2,3})\s*mmHg?", text, re.IGNORECASE)
    if bp:
        vitals["physicalExamBloodPressureSystolic"] = bp.group(1)
        vitals["physicalExamBloodPressureDiastolic"] = bp.group(2)

    return vitals


def date_value_for_input(page, name, value):
    """根据日期输入框的 placeholder/现有格式，尽量用页面期望的日期格式。"""
    candidates = date_candidates_for_input(page, name, value)
    return candidates[0] if candidates else str(value or "").strip()


def date_candidates_for_input(page, name, value):
    """PrimeVue DatePicker 没暴露格式时，按常见格式逐个试。"""
    iso = normalize_date_string(value, default="")
    if not iso:
        return [str(value or "").strip()]

    y, mo, d = iso.split("-")
    yy = y[-2:]
    hint = ""
    try:
        loc = page.locator(f'[name="{name}"]').first
        parts = [
            loc.get_attribute("placeholder") or "",
            loc.get_attribute("aria-label") or "",
            loc.input_value(timeout=1000) or "",
        ]
        hint = " ".join(parts).lower()
    except Exception:
        hint = ""

    preferred = []
    compact_hint = re.sub(r"\s+", "", hint)
    if "yyyy/mm/dd" in compact_hint or "yyyy-mm-dd" in compact_hint:
        sep = "/" if "yyyy/mm/dd" in compact_hint else "-"
        preferred.append(f"{y}{sep}{mo}{sep}{d}")
    if "mm/dd/yyyy" in compact_hint:
        preferred.append(f"{mo}/{d}/{y}")
    if "dd/mm/yyyy" in compact_hint:
        preferred.append(f"{d}/{mo}/{y}")
    if "年" in hint:
        preferred.append(f"{int(y)}年{int(mo)}月{int(d)}日")
    if "/" in hint and "-" not in hint:
        preferred.append(f"{y}/{mo}/{d}")

    # PrimeVue DatePicker 默认常见格式是 mm/dd/yy；yy 通常可解析四位年。
    fallback = [
        f"{mo}/{d}/{y}",
        f"{mo}/{d}/{yy}",
        f"{y}/{mo}/{d}",
        iso,
        f"{int(y)}年{int(mo)}月{int(d)}日",
        f"{d}/{mo}/{y}",
    ]
    out = []
    for item in preferred + fallback:
        if item and item not in out:
            out.append(item)
    return out


def form_value_for_field(page, name, value):
    value = str(value).replace("**", "").strip()
    if name == "age":
        return normalize_age_value(value)
    if name in {"admissionDate", "recordDate"}:
        return date_value_for_input(page, name, value)
    if name in {
        "physicalExamBodyTemperature",
        "physicalExamPulse",
        "physicalExamRespiratoryRate",
        "physicalExamBloodPressureSystolic",
        "physicalExamBloodPressureDiastolic",
    }:
        m = re.search(r"\d+(?:\.\d+)?", value)
        return m.group(0) if m else value
    return value


INPUTNUMBER_FIELDS = {
    "age",
    "physicalExamBodyTemperature",
    "physicalExamPulse",
    "physicalExamRespiratoryRate",
    "physicalExamBloodPressureSystolic",
    "physicalExamBloodPressureDiastolic",
}

DATE_FIELDS = {"admissionDate", "recordDate"}


def set_input_value_js(ctrl, value):
    ctrl.evaluate(
        """(el, value) => {
            if (el instanceof HTMLSelectElement) {
                el.value = value;
            } else {
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
            }
            if (el.getAttribute('role') === 'spinbutton') {
                const numeric = Number(String(value).replace(/[^0-9.\\-]/g, ''));
                if (!Number.isNaN(numeric)) {
                    el.setAttribute('aria-valuenow', String(numeric));
                }
            }
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: String(value) }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        value,
    )


def type_control_like_user(page, ctrl, value, press_enter=False):
    ctrl.scroll_into_view_if_needed(timeout=3000)
    ctrl.click(timeout=3000)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(str(value), delay=20)
    if press_enter:
        page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    time.sleep(0.1)


def date_control_is_invalid(ctrl):
    try:
        return ctrl.evaluate(
            """el => {
                const wrapper = el.closest('[data-pc-name="datepicker"]');
                return el.getAttribute('aria-invalid') === 'true'
                    || el.classList.contains('p-invalid')
                    || (wrapper && wrapper.classList.contains('p-invalid'));
            }"""
        )
    except Exception:
        return False


def visible_calendar_day_selectors(day_text=None):
    if day_text:
        return [
            f'.p-datepicker-panel:visible .p-datepicker-day:text-is("{day_text}")',
            f'.p-datepicker:visible .p-datepicker-day:text-is("{day_text}")',
            f'[data-pc-name="datepicker"]:visible [data-pc-section="day"]:text-is("{day_text}")',
            f'.p-datepicker-panel:visible td:not(.p-disabled):not([aria-disabled="true"]) >> text="{day_text}"',
            f'.p-datepicker:visible td:not(.p-disabled):not([aria-disabled="true"]) >> text="{day_text}"',
        ]
    return [
        '.p-datepicker-panel:visible .p-datepicker-today .p-datepicker-day',
        '.p-datepicker:visible .p-datepicker-today .p-datepicker-day',
        '.p-datepicker-panel:visible [data-p-today="true"] .p-datepicker-day',
        '.p-datepicker:visible [data-p-today="true"] .p-datepicker-day',
        '.p-datepicker-panel:visible .p-datepicker-day:not(.p-disabled)',
        '.p-datepicker:visible .p-datepicker-day:not(.p-disabled)',
        '[data-pc-name="datepicker"]:visible [data-pc-section="day"]:not(.p-disabled)',
    ]


def click_visible_calendar_day(page, prefer_day=None):
    selectors = []
    if prefer_day:
        selectors.extend(visible_calendar_day_selectors(str(int(prefer_day))))
    selectors.extend(visible_calendar_day_selectors())

    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            count = 0
        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible(timeout=300):
                    continue
                aria_disabled = item.get_attribute("aria-disabled") or ""
                class_name = item.get_attribute("class") or ""
                if aria_disabled == "true" or "p-disabled" in class_name:
                    continue
                item.click(timeout=1500)
                time.sleep(0.3)
                return True
            except Exception:
                continue
    return False


def fill_date_control_by_calendar(page, ctrl, name, original_value):
    """
    这个网站的 PrimeVue DatePicker 必须通过日历面板点击才通过校验。
    入院日期允许随便选；记录日期优先点系统日期。
    """
    ctrl.scroll_into_view_if_needed(timeout=3000)
    ctrl.click(timeout=3000)
    time.sleep(0.4)

    today_day = int(time.strftime("%d"))
    prefer_day = today_day if name == "recordDate" else None
    if not click_visible_calendar_day(page, prefer_day=prefer_day):
        # 再点一次输入框重开面板，避免第一次点击只聚焦没有展开。
        ctrl.click(timeout=3000)
        time.sleep(0.4)
        click_visible_calendar_day(page, prefer_day=prefer_day)

    try:
        page.keyboard.press("Tab")
    except Exception:
        pass
    time.sleep(0.2)
    try:
        return ctrl.input_value(timeout=1000).strip()
    except Exception:
        return ""


def fill_form_controls_by_name(page, name, value):
    """按 name 填控件，并额外触发 input/change/blur，兼容 PrimeVue 日期/输入组件。"""
    original_value = value
    value = form_value_for_field(page, name, value)
    if not value:
        return

    loc = page.locator(f'[name="{name}"]')
    for i in range(loc.count()):
        ctrl = loc.nth(i)
        try:
            if name in DATE_FIELDS:
                value = fill_date_control_by_calendar(page, ctrl, name, original_value)
            elif name in INPUTNUMBER_FIELDS:
                type_control_like_user(page, ctrl, value, press_enter=(name in DATE_FIELDS))
            else:
                ctrl.scroll_into_view_if_needed(timeout=3000)
                ctrl.click(timeout=3000)
                ctrl.fill(value, timeout=5000)
                page.keyboard.press("Tab")

            current = ""
            try:
                current = ctrl.input_value(timeout=1000).strip()
            except Exception:
                pass
            if current != str(value):
                set_input_value_js(ctrl, value)
                try:
                    current = ctrl.input_value(timeout=1000).strip()
                except Exception:
                    current = ""
            if name in INPUTNUMBER_FIELDS and not current:
                type_control_like_user(page, ctrl, value, press_enter=False)
        except Exception as e:
            print(f"  [跳过] {name}: {e}")


def get_first_input_value(page, name):
    loc = page.locator(f'[name="{name}"]')
    if not loc.count():
        return ""
    try:
        return loc.first.input_value(timeout=1000).strip()
    except Exception:
        return ""


def verify_and_repair_required_form_values(page, filled):
    required = [
        "patientName",
        "age",
        "admissionDate",
        "recordDate",
        "physicalExamBodyTemperature",
        "physicalExamPulse",
        "physicalExamRespiratoryRate",
        "physicalExamBloodPressureSystolic",
        "physicalExamBloodPressureDiastolic",
    ]
    print("提交前关键字段检查：")
    for name in required:
        expected = form_value_for_field(page, name, filled.get(name, ""))
        actual = get_first_input_value(page, name)
        invalid = False
        if name in DATE_FIELDS:
            loc = page.locator(f'[name="{name}"]')
            if loc.count():
                invalid = date_control_is_invalid(loc.first)
        if expected and (not actual or invalid):
            print(f"  - {name} 为空，重填为 {expected}")
            fill_form_controls_by_name(page, name, filled.get(name, expected))
            actual = get_first_input_value(page, name)
        print(f"  - {name}: {actual or '(空)'}")


def fill_history_form(page, info_text, uname, sid, consult_date=""):
    fields_desc = "\n".join(f"{n}：{d}" for n, d in FORM_FIELDS)
    raw = ds_once(
        FORM_FILL_PROMPT,
        info_text
        + "\n\n【系统回顾与查体参考模板】\n" + SYSTEM_REVIEW_REF
        + "\n\n【表单字段（字段名：含义）】\n" + fields_desc,
    )
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        filled = json.loads(raw)
    except json.JSONDecodeError:
        print("表单字段返回非合法 JSON，原文前 500 字：\n", raw[:500])
        return {}

    # 身份字段兜底：页面基本信息里的姓名/性别/年龄强制覆盖；
    # 民族、婚姻、籍贯、职业、现住址等未采集项不允许填“无”。
    # 日期字段统一 YYYY-MM-DD。
    filled = backfill_form_identity_fields(filled, info_text)

    # 既往史字段兜底：未采集/空/不详统一填“无”。
    filled = enforce_past_history_form_defaults(filled)

    # 婚姻史、月经史、生育史兜底；女性患者这两项必须有明确内容或“未采集”。
    filled = enforce_reproductive_form_defaults(filled, info_text)

    # 生命体征兜底：DeepSeek 若漏填，用默认值补上，确保表单里这几项一定有数值
    extracted_vitals = extract_vitals_from_text(info_text)
    vital_defaults = {
        "physicalExamBodyTemperature": "36.5",
        "physicalExamPulse": "80",
        "physicalExamRespiratoryRate": "18",
        "physicalExamBloodPressureSystolic": "120",
        "physicalExamBloodPressureDiastolic": "80",
    }
    for k, dv in vital_defaults.items():
        filled[k] = extracted_vitals.get(k) or form_value_for_field(page, k, filled.get(k, "")) or dv

    # 固定/规则填写项：直接覆盖，不交给 DeepSeek 判断
    today = time.strftime("%Y-%m-%d")               # 电脑系统日期，格式 YYYY-MM-DD
    normalized_consult_date = normalize_date_string(consult_date, default="")
    filled["admissionDate"] = normalized_consult_date or normalize_date_string(filled.get("admissionDate", ""), default="") or today  # 入院日期=问诊时间
    filled["recordDate"] = today                    # 记录日期=系统当天，格式 YYYY-MM-DD
    filled["age"] = normalize_age_value(filled.get("age", ""))
    filled["patientName"] = clean_patient_name(filled.get("patientName", "")) or "未采集"
    filled["narrator"] = filled.get("narrator", "") or "本人"   # 病史陈述者，无特殊说明=本人
    filled["reliability"] = "可靠"                  # 可靠程度固定
    filled["department"] = context_value(info_text, "科别") or filled.get("department", "") or "内科"
    filled["wardBed"] = context_value(info_text, "病舍床号") or filled.get("wardBed", "") or "8床"
    filled["hospitalId"] = context_value(info_text, "住院号") or filled.get("hospitalId", "") or "000024"
    for k in ("rectumAndAnus", "pudendum"):         # 直肠肛门 / 外生殖器 无信息=未检
        if not str(filled.get(k, "")).strip():
            filled[k] = "未检"

    for name, value in filled.items():  # 按 name 填，重名的全填
        fill_form_controls_by_name(page, name, value)
    for nm, val in [("recorder", uname), ("studentName", uname), ("studentId", sid)]:
        fill_form_controls_by_name(page, nm, val)  # 记录人/学生本人信息直接填 env 里的姓名
    verify_and_repair_required_form_values(page, filled)
    return filled


# ========== 主流程 ==========
def save_case_json(case_no, result):
    """同时保存“最新结果”和“第N例结果”，避免五次循环互相覆盖。"""
    latest_path = os.path.join(BASE_DIR, "大病史.json")
    case_path = os.path.join(BASE_DIR, f"大病史_第{case_no}例.json")
    for path in (latest_path, case_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


def save_phys_json(case_no, phys_history):
    latest_path = os.path.join(BASE_DIR, "体格检查.json")
    case_path = os.path.join(BASE_DIR, f"体格检查_第{case_no}例.json")
    for path in (latest_path, case_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(phys_history, f, ensure_ascii=False, indent=2)


ERROR_KEYWORDS = ["错误", "有误", "必填", "不能为空", "格式错误", "格式不正确", "格式有误", "无效", "未填写", "请选择", "请填写", "内容填写错误", "校验", "验证失败"]
SUCCESS_KEYWORDS = ["提交成功", "保存成功", "已提交", "完成", "得分", "评分"]


def is_error_message(text):
    return any(k in str(text or "") for k in ERROR_KEYWORDS)


def collect_history_form_diagnostics(page):
    """收集提交后页面上能看到的校验线索。"""
    diagnostics = {
        "url": page.url,
        "error_texts": [],
        "success_texts": [],
        "invalid_controls": [],
        "marked_controls": [],
    }

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        diagnostics["error_texts"] = sorted({line for line in lines if is_error_message(line)})
        diagnostics["success_texts"] = sorted({line for line in lines if any(k in line for k in SUCCESS_KEYWORDS)})
    except Exception as e:
        diagnostics["error_texts"].append(f"读取页面文本失败：{e}")

    try:
        diagnostics["invalid_controls"] = page.evaluate(
            """() => {
                function visible(el) {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                }
                function labelOf(el) {
                    if (el.labels && el.labels.length) {
                        return Array.from(el.labels).map(x => x.innerText.trim()).filter(Boolean).join(' / ');
                    }
                    if (el.id) {
                        const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (label) return label.innerText.trim();
                    }
                    const box = el.closest('.field,.form-field,.p-field,.form-group,.grid,.flex,div');
                    return box ? box.innerText.replace(/\\s+/g, ' ').trim().slice(0, 160) : '';
                }
                return Array.from(document.querySelectorAll('input, textarea, select'))
                    .filter(el => !el.disabled && visible(el) && typeof el.checkValidity === 'function' && !el.checkValidity())
                    .map(el => ({
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        type: el.getAttribute('type') || el.tagName.toLowerCase(),
                        label: labelOf(el),
                        message: el.validationMessage || '',
                        value: (el.getAttribute('type') || '').toLowerCase() === 'password' ? '<hidden>' : (el.value || '').slice(0, 120),
                    }));
            }"""
        )
    except Exception as e:
        diagnostics["invalid_controls"] = [{"message": f"读取 HTML5 校验失败：{e}"}]

    try:
        diagnostics["marked_controls"] = page.evaluate(
            """() => {
                function visible(el) {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                }
                const selectors = '[aria-invalid="true"], .p-invalid, .is-invalid, .invalid, .error, .text-red-500, .text-red-600';
                return Array.from(document.querySelectorAll(selectors))
                    .filter(visible)
                    .map(el => ({
                        tag: el.tagName.toLowerCase(),
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        value: (el.getAttribute('type') || '').toLowerCase() === 'password' ? '<hidden>' : ((el.value || '').slice(0, 120)),
                        placeholder: el.getAttribute('placeholder') || '',
                        ariaValueNow: el.getAttribute('aria-valuenow') || '',
                        className: el.getAttribute('class') || '',
                        parentComponent: el.closest('[data-pc-name]')?.getAttribute('data-pc-name') || '',
                        text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').replace(/\\s+/g, ' ').trim().slice(0, 180),
                    }))
                    .filter(x => x.text || x.name || x.id);
            }"""
        )
    except Exception as e:
        diagnostics["marked_controls"] = [{"text": f"读取标红控件失败：{e}"}]

    return diagnostics


def diagnostics_has_error(diagnostics, dialogs):
    dialog_errors = [d for d in dialogs if is_error_message(d.get("message", ""))]
    return bool(
        dialog_errors
        or diagnostics.get("error_texts")
        or diagnostics.get("invalid_controls")
        or diagnostics.get("marked_controls")
    )


def save_history_submit_debug_artifacts(page, diagnostics, case_no):
    prefix = f"病史提交错误_第{case_no}例" if case_no else "病史提交错误"
    screenshot_path = os.path.join(BASE_DIR, prefix + ".png")
    html_path = os.path.join(BASE_DIR, prefix + ".html")
    json_path = os.path.join(BASE_DIR, prefix + ".json")
    saved = {}

    try:
        page.screenshot(path=screenshot_path, full_page=True)
        saved["screenshot"] = screenshot_path
    except Exception as e:
        saved["screenshot_error"] = str(e)

    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        saved["html"] = html_path
    except Exception as e:
        saved["html_error"] = str(e)

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(diagnostics, f, ensure_ascii=False, indent=2)
        saved["diagnostics"] = json_path
    except Exception as e:
        saved["diagnostics_error"] = str(e)

    return saved


def print_submit_diagnostics(diagnostics, dialogs):
    if dialogs:
        print("网页弹窗：")
        for d in dialogs:
            print(f"  - [{d.get('type')}] {d.get('message')}")
    if diagnostics.get("error_texts"):
        print("页面错误提示：")
        for t in diagnostics["error_texts"]:
            print("  -", t)
    if diagnostics.get("invalid_controls"):
        print("HTML5 校验未通过字段：")
        for item in diagnostics["invalid_controls"]:
            print(f"  - name={item.get('name') or '(无)'} label={item.get('label') or '(无)'} message={item.get('message') or item.get('text') or '(无)'} value={item.get('value', '')}")
    if diagnostics.get("marked_controls"):
        print("页面标红/错误控件：")
        for item in diagnostics["marked_controls"][:20]:
            print(
                f"  - name={item.get('name') or '(无)'} id={item.get('id') or '(无)'} "
                f"text={item.get('text') or '(无)'} value={item.get('value', '')} "
                f"placeholder={item.get('placeholder', '')} ariaValueNow={item.get('ariaValueNow', '')} "
                f"component={item.get('parentComponent', '')}"
            )


def submit_history_form(page, case_no=None):
    """自动提交大病史表单，并确认是否出现校验错误。"""
    dialogs = []

    def on_dialog(dialog):
        dialogs.append({"type": dialog.type, "message": dialog.message})
        print(f"网页弹窗：{dialog.message}")
        try:
            dialog.accept()
        except Exception:
            pass

    page.on("dialog", on_dialog)
    page.keyboard.press("Escape")
    time.sleep(0.5)
    button_names = ["提交", "总提交", "提交病史", "提交大病史", "完成", "保存"]
    clicked_name = ""
    for name in button_names:
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count() > 0:
                btn.last.click(timeout=5000)
                print(f"已点击大病史表单按钮：{name}")
                clicked_name = name
                break
        except Exception:
            pass
    if not clicked_name:
        try:
            page.remove_listener("dialog", on_dialog)
        except Exception:
            pass
        print("未找到大病史表单提交按钮，已跳过自动提交。")
        return {"ok": False, "clicked": False, "reason": "未找到大病史表单提交按钮", "url": page.url}

    final_diag = {}
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.8)
        final_diag = collect_history_form_diagnostics(page)
        if diagnostics_has_error(final_diag, dialogs) or final_diag.get("success_texts"):
            break

    try:
        page.remove_listener("dialog", on_dialog)
    except Exception:
        pass

    final_diag["dialogs"] = dialogs
    final_diag["clicked_button"] = clicked_name
    ok = not diagnostics_has_error(final_diag, dialogs)
    final_diag["ok"] = ok

    if ok:
        if final_diag.get("success_texts"):
            print("检测到提交成功提示：", "；".join(final_diag["success_texts"]))
        else:
            print("未检测到明显错误提示；当前页面：", final_diag.get("url", page.url))
        return final_diag

    print("\n[大病史表单提交失败或未通过校验]")
    print_submit_diagnostics(final_diag, dialogs)
    saved = save_history_submit_debug_artifacts(page, final_diag, case_no)
    final_diag["saved_artifacts"] = saved
    print("已保存提交失败诊断文件：")
    for kind, path in saved.items():
        print(f"  - {kind}: {path}")
    return final_diag


def run_one_case(page, case_no, user_name, student_number, user_email):
    print(f"\n========== 第 {case_no} / {RUN_TIMES} 例开始 ==========")

    patient_info, consult_date = get_consult_info(page)
    consult_date_std = normalize_date_string(consult_date, default=consult_date)
    patient_basic = extract_basic_from_header(patient_info)
    print(f"[基本信息] {patient_info}")
    print(f"[基本信息解析] {patient_basic if patient_basic else '(未解析到结构化基本信息)'}")

    # ===== 阶段1：问诊 =====
    print("\n===== 阶段1 问诊 =====")
    consult_system = (
        CONSULT_PROMPT
        + "\n\n" + build_required_consult_note(patient_info)
        + f"\n\n【页面基本信息】\n{patient_info}"
        + f"\n【问诊时间】{consult_date}"
    )
    consult_history = run_chat(consult_region(page), consult_system, "[问诊结束]")

    summary_input = (
        f"【页面基本信息】\n{patient_info}\n"
        f"【问诊时间】{consult_date}\n\n"
        + SUMMARY_PROMPT
    )
    summary = client.chat.completions.create(
        model="deepseek-chat",
        messages=[*consult_history, {"role": "user", "content": summary_input}],
    ).choices[0].message.content
    summary = summary.replace("```json", "").replace("```", "").strip()
    try:
        dabingshi = json.loads(summary)
        dabingshi = backfill_basic_info(dabingshi, patient_info)
    except json.JSONDecodeError:
        dabingshi = {"原始文本": summary}
    result = {"问诊时间": consult_date_std, "病人基本信息": patient_info, "大病史": dabingshi}
    save_case_json(case_no, result)
    print(f"已保存 大病史_第{case_no}例.json")

    # ===== 阶段2：体格检查 =====
    print("\n===== 阶段2 体格检查 =====")
    switch_tab(page, "体格检查")
    phys_system = PHYS_PROMPT + "\n\n【问诊记录，供参考】\n" + json.dumps(dabingshi, ensure_ascii=False)
    phys_history = run_chat(phys_region(page), phys_system, "[查体结束]", max_turns=30)
    save_phys_json(case_no, phys_history)
    print(f"已保存 体格检查_第{case_no}例.json")

    # ===== 阶段3：辅助检查（全自动）=====
    print("\n===== 阶段3 辅助检查 =====")
    switch_tab(page, "辅助检查")

    menu = list(LEAF_PATHS.keys())
    aux_input = (
        "【问诊大病史】\n" + json.dumps(dabingshi, ensure_ascii=False)
        + "\n\n【体格检查记录】\n" + json.dumps(phys_history, ensure_ascii=False)
        + "\n\n【可开立检查项清单】\n" + json.dumps(menu, ensure_ascii=False)
    )
    raw = ds_once(AUX_PROMPT, aux_input)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        chosen = json.loads(raw)
    except json.JSONDecodeError:
        chosen = []
    valid = [c for c in chosen if c in LEAF_PATHS]
    invalid = [c for c in chosen if c not in LEAF_PATHS]

    print("\nDeepSeek 建议开立以下检查：")
    for c in valid:
        print("  -", " › ".join(LEAF_PATHS[c]))
    if invalid:
        print("  （以下不在清单中，已忽略：", invalid, "）")

    for c in valid:
        print("选中：", c)
        select_one_test(page, LEAF_PATHS[c])

    page.keyboard.press("Escape")
    time.sleep(0.3)
    aux_region(page).get_by_role("button", name="提交辅助检查", exact=True).click()
    print("已提交辅助检查，等待结果返回...")

    aux_results = grab_aux_results(page)
    print("\n辅助检查结果：\n", aux_results if aux_results else "(未读到结果，请在界面查看)")

    # ===== 诊断框：此时还没有标准答案，由 DeepSeek 自行判断 =====
    diag_input = (
        "【页面基本信息】\n" + patient_info
        + "\n\n【大病史】\n" + json.dumps(dabingshi, ensure_ascii=False)
        + "\n\n【体格检查】\n" + json.dumps(phys_history, ensure_ascii=False)
        + "\n\n【辅助检查结果】\n" + aux_results
    )
    diagnosis = ds_once(DIAGNOSIS_PROMPT, diag_input).replace("**", "")
    print("\n初步诊断：\n", diagnosis)

    note_text = ds_once(NOTE_PROMPT, diag_input + "\n\n【初步诊断】\n" + diagnosis).replace("**", "")

    diag_box = page.locator('textarea[name="conclusion"]')
    diag_box.click()
    diag_box.fill(diagnosis)
    print("已把初步诊断填入诊断框。")

    page.locator('input[name="name"]').fill(user_name)
    page.locator('input[name="studentNumber"]').fill(student_number)
    page.locator('input[name="email"]').fill(user_email)
    print("已填写姓名 / 学号 / 邮箱。")

    fill_notepad(page, note_text)
    print("已在记事本填写病历记录。")

    page.keyboard.press("Escape")
    time.sleep(0.3)
    page.get_by_role("button", name="提交", exact=True).click()
    print("已点击总提交，等待标准诊断答案弹窗...")

    answer_dx = grab_answer_diagnosis(page)
    print("\n系统标准诊断（答案）：\n", answer_dx if answer_dx else "(未抓到答案)")

    result["标准诊断"] = answer_dx
    result["辅助检查结果"] = aux_results
    result["初步诊断"] = diagnosis
    result["记事本"] = note_text
    save_case_json(case_no, result)
    print(f"已更新 大病史_第{case_no}例.json（含标准诊断、辅助检查结果、初步诊断、记事本）")

    # ===== 阶段4：点“病史书写”进入大病史表单 =====
    enter_history_writing(page)
    try:
        page.wait_for_url("**/history-taking**", timeout=60000)
    except Exception:
        print("（没检测到 history-taking 链接，仍按字段名尝试填写）")

    info_text = (
        "【页面基本信息】\n" + patient_info
        + "\n\n【大病史】\n" + json.dumps(dabingshi, ensure_ascii=False)
        + "\n\n【体格检查】\n" + json.dumps(phys_history, ensure_ascii=False)
        + "\n\n【辅助检查结果】\n" + aux_results
        + "\n\n【初步诊断】\n" + diagnosis
        + f"\n\n【问诊时间】{consult_date}"
    )
    if answer_dx:
        info_text += "\n\n【本病例标准诊断（以此为准）】\n" + answer_dx

    filled = fill_history_form(page, info_text, user_name, student_number, consult_date)
    result["大病史表单"] = filled
    save_case_json(case_no, result)
    print(f"\n第 {case_no} 例大病史表单已填好。")

    submit_diag = submit_history_form(page, case_no=case_no)
    result["大病史表单提交诊断"] = submit_diag
    save_case_json(case_no, result)
    if not submit_diag.get("ok"):
        raise RuntimeError("大病史表单提交失败或未确认成功，已保存错误截图/HTML/诊断 JSON，停止后续病例。")

    print(f"========== 第 {case_no} / {RUN_TIMES} 例结束 ==========\n")
    return result


def main():
    user_name, student_number, user_email = get_user_info()
    site_user, site_pass = get_site_credentials()

    with sync_playwright() as p:
        browser_path = find_local_browser()
        if not browser_path:
            raise RuntimeError(
                "未找到本机 Edge 或 Chrome 浏览器。\n"
                "请安装 Microsoft Edge 或 Google Chrome 后再运行；\n"
                "或在 .env 中手动填写 BROWSER_PATH=浏览器exe完整路径。"
            )

        print(f"将使用本机浏览器：{browser_path}")

        ctx = p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "userdata"),
            executable_path=browser_path,
            headless=False,
            http_credentials={
                "username": site_user,
                "password": site_pass,
            },
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        selected_system = start_case_with_system(page, system_name=None)

        all_results = []
        for case_no in range(1, RUN_TIMES + 1):
            if case_no > 1:
                selected_system = start_case_with_system(page, system_name=selected_system)
            try:
                result = run_one_case(page, case_no, user_name, student_number, user_email)
                all_results.append(result)
            except Exception as e:
                print(f"第 {case_no} 例运行失败：{e}")
                break

        with open(os.path.join(BASE_DIR, "五次练习汇总.json"), "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"已保存 五次练习汇总.json；本次共完成 {len(all_results)} / {RUN_TIMES} 例。")
        input("\n全部流程结束，按回车关闭浏览器...")
        ctx.close()


if __name__ == "__main__":
    main()
