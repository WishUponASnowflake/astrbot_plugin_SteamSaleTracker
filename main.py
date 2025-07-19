import requests
import logging
import json
import asyncio
from pathlib import Path
from rapidfuzz import process, fuzz
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
import astrbot.api.message_components as Comp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

@register("astrbot_plugin_SteamSaleTracker", "bushikq", "一个监控steam游戏价格变动的astrbot插件", "1.0.0")
class SteamSaleTrackerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_SteamSaleTracker"))
        self.plugin_dir = Path(__file__).resolve().parent
        self.json1_path = self.data_dir / "game_list.json" # 存储所有Steam游戏英文名与id对应的字典
        self.json2_path = self.data_dir / "monitor_list.json" # 存储用户或群组的监控列表

        # 确保数据文件存在，如果不存在则创建空文件
        if not self.json1_path.exists():
            with open(self.json1_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        if not self.json2_path.exists():
            with open(self.json2_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)

        self.config = config
        # 从配置中获取是否启用日志输出，默认为 False
        self.enable_log_output = self.config.get("enable_log_output", False)
        # 从配置中获取价格检查间隔时间，默认为 30 分钟
        self.interval_minutes = self.config.get("interval_minutes", 30)
        
        self.logger = logging.getLogger("astrbot_plugin_SteamSaleTracker")
        # 配置日志处理器，避免重复添加
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        # 根据配置设置日志级别
        self.logger.setLevel(logging.INFO if self.enable_log_output else logging.ERROR)
        self.logger.info("正在初始化SteamSaleTracker插件")

        self.scheduler = AsyncIOScheduler()
        # 添加定时任务，每隔 interval_minutes 运行 run_monitor_prices 方法
        self.scheduler.add_job(self.run_monitor_prices, "interval", minutes=self.interval_minutes)
        self.scheduler.start() # 启动调度器

        self.monitor_list_lock = asyncio.Lock() # 用于保护 monitor_list 文件的读写
        asyncio.create_task(self.initialize_data()) # 异步初始化数据，避免阻塞主线程

    async def initialize_data(self):
        """异步初始化数据（避免阻塞主线程），获取游戏列表和加载用户监控列表"""
        await self.get_app_list()  # 获取Steam全量游戏列表
        await self.load_user_monitors() # 加载用户监控列表

    async def get_app_list(self):
        """获取Steam全量游戏列表（AppID + 名称），并缓存到 game_list.json"""
        try:
            url = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
            res = requests.get(url).json()
            self.app_dict_all = {app["name"]: app["appid"] for app in res["applist"]["apps"]}
            with open(self.json1_path, "w", encoding="utf-8") as f: 
                json.dump(self.app_dict_all, f, ensure_ascii=False, indent=4)
            self.logger.info("Steam游戏列表更新成功")
        except Exception as e:
            self.logger.error(f"获取游戏列表失败：{e}")
            self.app_dict_all = {} # 失败时初始化为空字典

    async def load_user_monitors(self):
        """加载用户监控列表（从 monitor_list.json 文件）"""
        try:
            async with self.monitor_list_lock: # 加锁读取，防止文件被其他操作同时修改
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    self.monitor_list = json.load(f)
            self.logger.info("监控列表加载成功")
        except FileNotFoundError or json.JSONDecodeError as e:
            self.monitor_list = {}  # 文件不存在或者文件损坏时初始化为空字典
            self.logger.info("文件不存在或者文件损坏，已创建空列表:{e}")
            with open(self.json2_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    async def get_appid_by_name(self, user_input, target_dict: dict = None):
        """
        模糊匹配游戏名到AppID。
        Args:
            user_input (str): 用户输入的待匹配游戏名。
            target_dict (dict): 用于匹配的目标字典 (游戏名: AppID)。
        Returns:
            list or None: 如果找到匹配项，返回 [AppID, 匹配的游戏名]，否则返回 None。
        """
        self.logger.info(f"正在模糊匹配游戏名: {user_input}")
        if not target_dict:
            self.logger.warning("target_dict 为空，无法进行模糊匹配。")
            return None
        
        matched_result = process.extractOne(user_input, target_dict.keys(), scorer=fuzz.token_set_ratio)
        if matched_result and matched_result[1] >= 70:
            matched_name = matched_result[0]
            return [target_dict[matched_name], matched_name]
        else:
            return None
    
    async def get_steam_price(self, appid, region="cn"):
        """
        获取游戏价格信息。
        Args:
            appid (str or int): Steam 游戏的 AppID。
            region (str): 区域代码，默认为 "cn" (中国)。
        Returns:
            dict or None: 包含价格信息的字典，或 None（如果获取失败或游戏不存在）。
        """
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l=zh-cn"
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: requests.get(url).json())

            data = res.get(str(appid))
            if not data or not data.get("success"):
                self.logger.warning(f"获取游戏 {appid} 价格失败或游戏不存在，data: {data}")
                return None
            
            game_data = data["data"]
            if game_data.get("is_free"):  # 免费游戏
                return {"is_free": True, "current_price": 0, "original_price": 0, "discount": 100, "currency": "FREE"}
            
            # 有些游戏可能没有价格信息（比如即将发售），需要检查
            price_info = game_data.get("price_overview")
            if not price_info:
                self.logger.info(f"游戏 {game_data.get('name', appid)} 没有价格信息 (可能即将发售或未在 {region} 区域上架)。")
                return None

            return {
                "is_free": False,
                "current_price": price_info["final"] / 100, # 单位转换为元
                "original_price": price_info["initial"] / 100,
                "discount": price_info["discount_percent"],
                "currency": price_info["currency"]  # 货币类型
            }
        except Exception as e:
            self.logger.error(f"获取游戏 {appid} 价格时发生异常：{e}")
            return None
    
    async def monitor_prices(self):
        """
        定时检查监控列表中游戏的价格变动。
        如果价格有变动，则 yield 通知信息。
        Yields:
            tuple: (target_type, target_id, at_members_list, msg_components)
                   target_type: "user" (私聊) 或 "group" (群聊)
                   target_id: 接收消息的用户ID或群组ID
                   at_members_list: 需要在群聊中 @ 的用户ID列表 (私聊时为空)
                   msg_components: 消息组件列表
        """
        async with self.monitor_list_lock:
            try:
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    current_monitor_list = json.load(f)
            except json.JSONDecodeError or FileNotFoundError as e:
                self.logger.error(f"监控列表文件解析失败或不尊在，已重置为空列表: {e}")
                current_monitor_list = {}

        games_to_check = list(current_monitor_list.items()) 

        for game_id, game_info in games_to_check:
            self.logger.info(f"正在检查游戏: {game_info['name']} (AppID: {game_id})")
            price_data = await self.get_steam_price(game_id, game_info.get("region", "cn")) # 允许存储不同区域
            
            if not price_data:
                self.logger.warning(f"无法获取游戏《{game_info.get('name', game_id)}》的价格信息，跳过此次检查。")
                continue

            # 首次设置价格，初始化 last_price 等数据
            if game_info["last_price"] is None:
                current_monitor_list[game_id]["last_price"] = price_data["current_price"]
                current_monitor_list[game_id]["original_price"] = price_data["original_price"]
                current_monitor_list[game_id]["discount"] = price_data["discount"]
                async with self.monitor_list_lock:
                    with open(self.json2_path, "w", encoding="utf-8") as f:
                        json.dump(current_monitor_list, f, ensure_ascii=False, indent=4)
                self.logger.info(f"游戏《{game_info.get('name', game_id)}》首次记录价格：¥{price_data['current_price']:.2f}")
                continue # 首次记录不发送通知

            price_change = price_data["current_price"] - game_info["last_price"]

            # 如果价格有变动
            if price_change != 0:
                self.logger.info(f"游戏《{game_info.get('name', game_id)}》价格变动！")
                msg_components = []
                
                if price_data["is_free"]:
                    msg_components.append(Comp.Plain(text=f"🎉🎉🎉游戏《{game_info['name']}》已免费！\n"))
                elif price_change > 0:
                    msg_components.append(Comp.Plain(text=f"⬆️游戏《{game_info['name']}》价格上涨：¥{price_change:.2f}\n"))
                elif price_change < 0:
                    msg_components.append(Comp.Plain(text=f"⬇️游戏《{game_info['name']}》价格下跌：¥{-price_change:.2f}\n"))
                
                msg_components.append(Comp.Plain(text=f"变动前价格：¥{game_info['last_price']:.2f}，当前价：¥{price_data['current_price']:.2f}，原价：¥{price_data['original_price']:.2f}，对比原价折扣：{price_data['discount']}%\n"))
                msg_components.append(Comp.Plain(text=f"购买链接：https://store.steampowered.com/app/{game_id}\n"))
                
                # 更新内存中的监控列表
                current_monitor_list[game_id]["last_price"] = price_data["current_price"]
                current_monitor_list[game_id]["original_price"] = price_data["original_price"]
                current_monitor_list[game_id]["discount"] = price_data["discount"]
                
                # 立即将更新后的监控列表写入文件
                async with self.monitor_list_lock:
                    with open(self.json2_path, "w", encoding="utf-8") as f:
                        json.dump(current_monitor_list, f, ensure_ascii=False, indent=4)
                
                # 遍历所有订阅者，确定通知目标
                for subscriber in game_info.get("subscribers", []):
                    if subscriber["type"] == "user":
                        # 个人用户，发送私聊
                        yield "user", subscriber["id"], [], msg_components 
                    elif subscriber["type"] == "group":
                        # 群组，发送群聊并 @ 群内订阅该游戏的成员
                        at_members = subscriber.get("member_ids", []) # 这里存储了群里订阅者的用户ID
                        yield "group", subscriber["id"], at_members, msg_components
            else:
                self.logger.info(f"游戏《{game_info.get('name', game_id)}》价格未变动")
    
    async def run_monitor_prices(self):
        """定时任务的wrapper函数（迭代生成器并发送消息）"""
        self.logger.info("开始执行价格检查任务")
        try:
            # 迭代 monitor_prices 生成器，获取所有待发送的消息
            async for target_type, target_id, at_members, msg_components in self.monitor_prices():
                if not target_id or not msg_components:
                    continue
                
                if target_type == "user":
                    final_message_for_user = msg_components# 私聊消息，不需要 @ 成员
                    self.logger.info(f"正在向用户 {target_id} 发送价格变动通知。")
                    await self.context.send_message(
                        user_id=target_id, 
                        message=final_message_for_user,
                    )
                elif target_type == "group":
                    final_message_for_group = msg_components
                    # 为所有需要 @ 的群成员添加 @ 组件
                    if at_members:
                        for member_id in at_members:
                            final_message_for_group.append(Comp.At(qq=member_id))
                    else: # 如果没有指定 @ 成员，但想让群里的人看到，可以考虑 @ 所有人或直接发送
                        self.logger.warning(f"群组 {target_id} 订阅的游戏《{final_message_for_group[0].text.split('《')[1].split('》')[0]}》没有指定@成员，消息将直接发送到群里。")

                    self.logger.info(f"正在向群组 {target_id} 发送价格变动通知。")
                    await self.context.send_message(
                        group_id=target_id, # 发送给这个群组
                        message=final_message_for_group,
                    )
                await asyncio.sleep(1) # 增加1s延迟，避免被风控
            self.logger.info("价格检查任务执行完成")
        except Exception as e:
            self.logger.error(f"价格检查任务失败：{e}")

    @filter.command("steamrmd", alias={'steam订阅', 'steam订阅游戏'})
    async def steamremind_command(self, event: AstrMessageEvent):
        """
        创建游戏监控。
        若游戏价格变动则提醒，群组订阅在群内提醒，个人订阅私聊提醒。
        """
        args = event.message_str.strip().split()[1:]
        if len(args) < 1:
            yield event.plain_result("请输入游戏名，例如：/steam订阅 赛博朋克2077")
            return
        
        region = "cn" # 目前暂时只支持国区，之后可以拓展配置选项
        app_name = " ".join(args)
        
        yield event.plain_result(f"正在搜索 {app_name}，请稍候...") 
        game_info_list = await self.get_appid_by_name(app_name, self.app_dict_all)
        self.logger.info(f"搜索结果 game_info_list: {game_info_list}")
        
        if not game_info_list:
            yield event.plain_result(f"未找到《{app_name}》，请检查拼写或尝试更精确的名称。")
            return
        
        game_id, game_name = game_info_list
        sender_id = str(event.get_sender_id())
        group_id = str(event.get_group_id()) if event.get_group_id() else None # 获取群组ID，如果不在群里则为None
        print(event.unified_msg_origin)

        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)
            
            self.logger.info(f"读取 monitor_list 后: {monitor_list}")
            game_id = str(game_id)
            
            # 如果游戏不在监控列表中，则先添加游戏的基本信息
            if game_id not in monitor_list:
                monitor_list[game_id] = {
                    "name": game_name,
                    "appid": game_id,
                    "region": region,
                    "last_price": None, # 初始为 None，让定时任务去获取并初始化
                    "original_price": None,
                    "discount": None,
                    "subscribers": [] # 存储订阅者信息，每个元素是 {"type": "user" or "group", "id": "xxx", "member_ids": [...]}
                }
            
            already_subscribed = False
            for sub_info in monitor_list[game_id]["subscribers"]:
                if group_id and sub_info["type"] == "group" and sub_info["id"] == group_id:
                    # 群组已订阅，检查当前用户是否已在群组的订阅成员中
                    if sender_id in sub_info.get("member_ids", []):
                        yield event.plain_result(f"您已在本群组中订阅《{game_name}》，无需重复订阅。")
                        already_subscribed = True
                        break
                    else: # 群组已订阅，但当前用户首次在该群组订阅
                        sub_info.setdefault("member_ids", []).append(sender_id)
                        yield event.plain_result(f"已成功将您添加到《{game_name}》的群组订阅列表。")
                        already_subscribed = True # 视为已订阅，但更新了成员列表
                        break
                elif not group_id and sub_info["type"] == "user" and sub_info["id"] == sender_id:
                    # 个人已订阅
                    already_subscribed = True
                    yield event.plain_result(f"您已订阅《{game_name}》，无需重复订阅。")
                    break
            if not already_subscribed: # 全新订阅
                if group_id: # 群组订阅
                    monitor_list[game_id]["subscribers"].append({"type": "group", "id": group_id, "member_ids": [sender_id]})
                    yield event.plain_result(f"已成功在当前群组订阅《{game_name}》，价格变动将在群内通知并 @ 您。")
                else: # 个人订阅 (私聊)
                    monitor_list[game_id]["subscribers"].append({"type": "user", "id": sender_id})
                    yield event.plain_result(f"已成功订阅《{game_name}》，价格变动将私聊通知您。")
            
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump(monitor_list, f, ensure_ascii=False, indent=4) # 写入时加入 indent 和 ensure_ascii=False 提高可读性
        
        await self.run_monitor_prices() # 订阅或更新后，立即重新检查一次价格。

    @filter.command("delsteamrmd",alias={'steam取消订阅', 'steam取消订阅游戏','steam删除订阅'})
    async def steamrmdremove_command(self, event: AstrMessageEvent):
        """删除游戏监控，不再提醒"""
        args = event.message_str.strip().split()[1:]
        if len(args) < 1:
            yield event.plain_result("请输入游戏名，例如：/steam取消订阅 赛博朋克2077")
            return
        app_name = " ".join(args)
        
        yield event.plain_result(f"正在搜索 {app_name}，请稍候...")
        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                current_monitor_list_for_search = json.load(f)
            self.app_dict_subscribed = {
                current_monitor_list_for_search[app_id]["name"]: current_monitor_list_for_search[app_id]["appid"] 
                for app_id in current_monitor_list_for_search
            }

        game_info_list = await self.get_appid_by_name(app_name, self.app_dict_subscribed)
        self.logger.info(f"搜索结果 game_info_list: {game_info_list}")
        
        if not game_info_list:
            yield event.plain_result(f"未找到《{app_name}》在您的订阅列表中，请检查拼写或尝试更精确的名称。")
            return
        
        game_id, game_name = game_info_list
        sender_id = str(event.get_sender_id())
        group_id = str(event.get_group_id()) if event.get_group_id() else None
        game_id = str(game_id)

        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)
            
            self.logger.info(f"读取 monitor_list 后: {monitor_list}")
            
            if game_id not in monitor_list:
                yield event.plain_result(f"《{game_name}》未被订阅，无需取消。")
                return

            found_and_removed = False
            updated_subscribers = []
            
            # 遍历并更新订阅者列表
            for sub_info in monitor_list[game_id]["subscribers"]:
                if group_id and sub_info["type"] == "group" and sub_info["id"] == group_id:
                    # 尝试从群组的成员列表中移除当前用户
                    if sender_id in sub_info.get("member_ids", []):
                        sub_info["member_ids"].remove(sender_id)
                        self.logger.info(f"用户 {sender_id} 已从群组 {group_id} 的《{game_name}》订阅者中移除。")
                        found_and_removed = True
                    
                    if sub_info.get("member_ids"): # 如果群组仍有订阅成员，则保留该群组订阅条目
                        updated_subscribers.append(sub_info)
                    else: # 如果群组已无订阅成员，则移除该群组的订阅条目
                        self.logger.info(f"群组 {group_id} 已无《{game_name}》订阅者，移除群组订阅条目。")
                elif not group_id and sub_info["type"] == "user" and sub_info["id"] == sender_id:
                    # 私聊订阅，直接移除
                    self.logger.info(f"用户 {sender_id} 的《{game_name}》个人订阅已移除。")
                    found_and_removed = True
                else: # 保留其他订阅（无论是其他群组还是其他个人）
                    updated_subscribers.append(sub_info)

            if found_and_removed:
                monitor_list[game_id]["subscribers"] = updated_subscribers
                if not monitor_list[game_id]["subscribers"]: # 如果一个游戏没有任何订阅者了，则完全移除该游戏
                    del monitor_list[game_id]
                    self.logger.info(f"游戏《{game_name}》已无任何订阅者，从监控列表中移除。")
                yield event.plain_result(f"已成功将您从《{game_name}》的订阅列表中移除。")
            else:
                yield event.plain_result(f"您尚未订阅《{game_name}》，无需取消订阅。")
            
            self.logger.info(f"写入 monitor_list 前: {monitor_list}")
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump(monitor_list, f, ensure_ascii=False, indent=4)
        self.monitor_list = monitor_list # 更新内存中的监控列表

    @filter.command("steamrmdlist",alias={'steam订阅列表', 'steam订阅游戏列表'})
    async def steamremind_list_command(self, event: AstrMessageEvent):
        """查看当前用户或群组已订阅游戏列表"""
        sender_id = str(event.get_sender_id()) 
        group_id = str(event.get_group_id()) if event.get_group_id() else None
        
        # 加锁读取，确保获取到最新数据
        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)
            
        user_or_group_monitored_games = {}
        for game_id, game_info in monitor_list.items():
            for subscriber in game_info.get("subscribers", []):
                if group_id and subscriber["type"] == "group" and subscriber["id"] == group_id:
                    # 如果在群聊中查询，且该群组订阅了此游戏
                    if sender_id in subscriber.get("member_ids", []):# 只显示该群里当前用户订阅的游戏
                        user_or_group_monitored_games[game_id] = game_info
                    break # 找到群组订阅，就显示
                elif not group_id and subscriber["type"] == "user" and subscriber["id"] == sender_id:
                    # 如果在私聊中查询，且该用户订阅了此游戏
                    user_or_group_monitored_games[game_id] = game_info
                    break # 找到个人订阅，就显示

        if not user_or_group_monitored_games:
            yield event.plain_result("暂无已订阅游戏。")
            return
        
        count = 0
        message_parts = [Comp.Plain(text="您已订阅的游戏列表：\n")]
        for game_id, game_info in user_or_group_monitored_games.items():
            game_name = game_info.get('name', '未知游戏')
            last_price = game_info.get('last_price', '未初始化')
            original_price = game_info.get('original_price', 'N/A')
            discount = game_info.get('discount', 'N/A')
            count += 1
            
            message_parts.append(Comp.Plain(text=f"{count}.《{game_name}》 (AppID: {game_id})\n"))
            # 格式化价格显示，如果是非数字则显示原始值
            message_parts.append(Comp.Plain(text=f"  - 当前缓存价格：¥{last_price:.2f}\n" if isinstance(last_price, (int, float)) else f"  - 当前缓存价格：{last_price}\n"))
            message_parts.append(Comp.Plain(text=f"  - 原价：¥{original_price:.2f}\n" if isinstance(original_price, (int, float)) else f"  - 原价：{original_price}\n"))
            message_parts.append(Comp.Plain(text=f"  - 折扣：{discount}%\n" if isinstance(discount, (int, float)) else f"  - 折扣：{discount}\n"))
            message_parts.append(Comp.Plain(text=f"  - 链接：https://store.steampowered.com/app/{game_id}\n\n"))
            
        yield event.chain_result(message_parts)