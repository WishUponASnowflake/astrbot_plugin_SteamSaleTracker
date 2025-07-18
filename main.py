import requests
import logging
import json
import time
import asyncio
from pathlib import Path
from fuzzywuzzy import process
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
import astrbot.api.message_components as Comp
from apscheduler.schedulers.asyncio import AsyncIOScheduler


@register("astrbot_plugin_SteamSaleTracker", "bushikq", "一个监控steam游戏价格变动的astrbot插件", "1.0.0")
class SteamInfoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_SteamSaleTracker"))
        self.plugin_dir = Path(__file__).resolve().parent
        self.json1_path = self.data_dir / "game_list.json"
        self.json2_path = self.data_dir / "monitor_list.json"
        if not self.json2_path.exists():
            with open(self.json2_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        if not self.json1_path.exists():
            with open(self.json1_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        self.config = config
        self.enable_log_output = self.config.get("enable_log_output", False)
        self.interval_minutes = self.config.get("interval_minutes", 30)
        self.logger = logging.getLogger("astrbot_plugin_SteamSaleTracker")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO if self.enable_log_output else logging.ERROR)
        self.logger.info("正在初始化SteamSaleTracker插件")

        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(self.run_monitor_prices, "interval", minutes=self.interval_minutes)  # 每30分钟运行一次
        self.scheduler.start()

        self.monitor_list_lock = asyncio.Lock()# 确保异步锁已初始化
        asyncio.create_task(self.initialize_data())  # 异步初始化数据

    async def initialize_data(self):
        """异步初始化数据（避免阻塞主线程）"""
        await self.get_app_list()  # 获取Steam全量游戏列表
        await self.load_user_monitors() # 加载用户监控列表

    async def get_app_list(self):
        """获取Steam全量游戏列表（AppID + 名称）"""
        try:
            url = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
            res = requests.get(url).json()
            self.app_dict_all = {app["name"]: app["appid"] for app in res["applist"]["apps"]}
            with open(self.json1_path, "w", encoding="utf-8") as f: 
                json.dump(self.app_dict_all, f, ensure_ascii=False, indent=4)
            self.logger.info("Steam游戏列表更新成功")
        except Exception as e:
            self.logger.error(f"获取游戏列表失败：{e}")
            self.app_dict_all = {}

    async def load_user_monitors(self):
        """加载用户监控列表（从文件）"""
        try:
            async with self.monitor_list_lock: # 加锁读取
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    self.monitor_list = json.load(f)
            self.logger.info("监控列表加载成功")
        except FileNotFoundError:
            self.monitor_list = {}  # 初始化为空字典
            self.logger.info("监控列表文件不存在，已创建空列表")

    async def get_appid_by_name(self, user_input, app_dict=None):
        """模糊匹配游戏名到AppID"""
        # 确保 app_dict 已加载
        if not hasattr(self, 'app_dict') or not app_dict:#如果初始化失败
            await self.get_app_list() # 尝试重新加载
            if not self.app_dict_all: # 如果还是没有，则返回None
                return None
        
        matched_name, score = process.extractOne(user_input, app_dict.keys())
        return [self.app_dict_all[matched_name], matched_name] if score >= 70 else None
    
    async def get_steam_price(self, appid, region="cn"):
        """获取游戏价格信息"""
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l=zh-cn"
            res = requests.get(url).json()
            data = res[str(appid)]
            
            if not data["success"]:  # 游戏不存在或数据未找到
                return None
            
            game_data = data["data"]
            if game_data["is_free"]:  # 免费游戏
                return {"is_free": True, "current_price": 0, "original_price": 0, "discount": 100, "currency": "FREE"}
            
            # 提取价格信息（单位转换为元）
            if "price_overview" not in game_data: # 有些游戏可能没有价格信息（比如即将发售）
                return None

            price_info = game_data["price_overview"]
            return {
                "is_free": False,
                "current_price": price_info["final"] / 100, 
                "original_price": price_info["initial"] / 100,
                "discount": price_info["discount_percent"],
                "currency": price_info["currency"]  # 货币类型
            }
        except Exception as e:
            self.logger.error(f"获取游戏{appid}价格失败：{e}")
            return None
    
    async def monitor_prices(self):
        """定时检查价格任务"""
        async with self.monitor_list_lock:
            try:
                with open(self.json2_path, "r", encoding="utf-8") as f:
                    current_monitor_list = json.load(f)
            except:
                self.logger.error("监控列表文件读取失败，已重置为空列表")
                current_monitor_list = {}
        games_to_check = list(current_monitor_list.items()) 

        for game_id, game_info in games_to_check:
            self.logger.info(f"正在检查游戏: {game_info["name"]}")
            price_data = await self.get_steam_price(game_id)
            if not price_data:
                self.logger.error(f"游戏{game_info.get('name', game_id)}不存在或数据未找到")
                continue
            if game_info["last_price"] is None:
                current_monitor_list[game_id]["last_price"] = price_data["current_price"]
                # 首次设置价格，直接更新文件，不发送通知
                async with self.monitor_list_lock:
                    with open(self.json2_path, "w", encoding="utf-8") as f:
                        json.dump(current_monitor_list, f, ensure_ascii=False, indent=4)
                self.logger.info(f"游戏《{game_info.get('name', game_id)}》首次记录价格：¥{price_data['current_price']:.2f}")
                continue # 跳过本次通知

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
                current_monitor_list[game_id]["original_price"] = price_data["original_price"] # 补充保存原价和折扣
                current_monitor_list[game_id]["discount"] = price_data["discount"] # 补充保存原价和折扣
                
                # 立即将更新后的监控列表写入文件
                async with self.monitor_list_lock:
                    with open(self.json2_path, "w", encoding="utf-8") as f:
                        json.dump(current_monitor_list, f, ensure_ascii=False, indent=4)
                
                # Yield 通知给所有订阅该游戏的用户
                # `user_id` 是接收者列表，`msg_components` 是消息内容
                yield game_info["user_id"], msg_components 

            else:
                self.logger.info(f"游戏《{game_info.get('name', game_id)}》价格未变动")
    
    async def run_monitor_prices(self):
        """定时任务的wrapper函数（迭代生成器并发送消息）"""
        self.logger.info("开始执行价格检查任务")
        try:
            # 迭代monitor_prices生成器，获取所有待发送的消息
            # 这里会正确地解构 `user_ids` 和 `msg_components`
            async for user_ids, msg_components in self.monitor_prices():
                if not user_ids or not msg_components:
                    continue
                
                # 对于每个需要接收通知的用户，发送消息
                for user_id in user_ids:
                    # 在发送消息时添加 @ 组件
                    final_message_for_user = msg_components + [Comp.At(qq=user_id)]
                    self.logger.info(f"正在向用户 {user_id} 发送价格变动通知。")
                    await self.context.send_message(
                        user_id=user_id,
                        message=final_message_for_user,
                        # 如果需要支持群聊，这里需要根据event.group_id或其他方式获取group_id
                        # 并在 monitor_list 中保存 group_id
                        # 例如：group_id=game_info.get('group_id')
                    )
                    await asyncio.sleep(1) # 增加延迟，避免发送过快被风控
            self.logger.info("价格检查任务执行完成")
        except Exception as e:
            self.logger.error(f"价格检查任务失败：{e}")

    @filter.command("steamrmd", alias={'steam订阅', 'steam订阅游戏'})
    async def steamremind_command(self, event: AstrMessageEvent):
        """创建游戏监控，若游戏价格变动则提醒"""
        args = event.message_str.strip().split()[1:]
        if len(args) < 1:
            yield event.plain_result("请输入游戏名，例如：/steam订阅 赛博朋克2077")
            return
        
        region = "cn" #暂时只支持国区，之后可能会拓展
        app_name = " ".join(args)
        
        yield event.plain_result(f"正在搜索 {app_name}，请稍候...")
        
        # 暂时修改 app_dict 用于测试，实际应使用真实的Steam API数据
        # self.app_dict_all = {"The Binding of Isaac": 113200} 
        
        game_info_list = await self.get_appid_by_name(app_name, self.app_dict_all)
        self.logger.info(f"搜索结果 game_info_list: {game_info_list}")
        
        if not game_info_list:
            yield event.plain_result(f"未找到《{app_name}》，请检查拼写或尝试更精确的名称。")
            return
        
        game_id, game_name = game_info_list
        sender_id = event.get_sender_id()

        # 加锁进行文件读写，确保原子性
        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)
            
            self.logger.info(f"读取 monitor_list 后: {monitor_list}")
            game_id = str(game_id)
            if game_id not in monitor_list:
                # 首次添加游戏时，last_price 设为 None，让定时任务去获取并初始化
                monitor_list[game_id] = {
                    "name": game_name,
                    "appid": game_id,
                    "region": region,
                    "last_price": None, # 初始为 None，让定时任务去获取
                    "original_price": None, # 初始为 None
                    "discount": None, # 初始为 None
                    "user_id": [sender_id]
                }
                yield event.plain_result(f"已成功订阅《{game_name}》，系统将在下次价格检查时初始化价格并监控变动。")
            elif sender_id in monitor_list[game_id]["user_id"]:
                yield event.plain_result(f"您已订阅《{game_name}》，无需重复订阅。")
                return # 已经订阅，直接返回
            else:
                monitor_list[game_id]["user_id"].append(sender_id)
                yield event.plain_result(f"已成功将您添加到《{game_name}》的订阅列表。")
            
            self.logger.info(f"写入 monitor_list 前: {monitor_list}")
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump(monitor_list, f, ensure_ascii=False, indent=4) # 写入时加入 indent 和 ensure_ascii=False 提高可读性
    @filter.command("delsteamrmd",alias={'steam取消订阅', 'steam取消订阅游戏'})
    async def steamrmdremove_command(self, event: AstrMessageEvent):
        """创建游戏监控，若游戏价格变动则提醒"""
        args = event.message_str.strip().split()[1:]
        if len(args) < 1:
            yield event.plain_result("请输入游戏名，例如：/steam取消订阅 赛博朋克2077")
            return
        
        region = "cn" #暂时只支持国区，之后可能会拓展
        app_name = " ".join(args)
        
        yield event.plain_result(f"正在搜索 {app_name}，请稍候...")
        
        # 暂时修改 app_dict 用于测试，实际应使用真实的Steam API数据
        # self.app_dict_all = {"The Binding of Isaac": 113200} 
        self.app_dict_all = {app["name"]: app["appid"] for app in self.monitor_list}

        game_info_list = await self.get_appid_by_name(app_name, self.app_dict_subscribed)
        self.logger.info(f"搜索结果 game_info_list: {game_info_list}")
        
        if not game_info_list:
            yield event.plain_result(f"未找到《{app_name}》，请检查拼写或尝试更精确的名称。")
            return
        
        game_id, game_name = game_info_list
        sender_id = event.get_sender_id()

        # 加锁进行文件读写，确保原子性
        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)
            
            self.logger.info(f"读取 monitor_list 后: {monitor_list}")
            game_id = str(game_id)
            if game_id not in monitor_list or sender_id not in monitor_list[game_id]["user_id"]:
                yield event.plain_result(f"您尚未订阅《{game_name}》，无需取消订阅。")
            else:
                monitor_list[game_id]["user_id"].remove(sender_id)
                yield event.plain_result(f"已成功将您从《{game_name}》的订阅列表中移除。")
                return # 已经订阅，直接返回
            self.logger.info(f"写入 monitor_list 前: {monitor_list}")
            with open(self.json2_path, "w", encoding="utf-8") as f:
                json.dump(monitor_list, f, ensure_ascii=False, indent=4)

    @filter.command("steamrmdlist",alias={'steam订阅列表', 'steam订阅游戏列表'})
    async def steamremind_list_command(self, event: AstrMessageEvent):
        """查看已订阅游戏列表"""
        sender_id = event.get_sender_id() # 使用 get_sender_id() 获取发送者ID
        
        # 加锁读取，确保获取到最新数据
        async with self.monitor_list_lock:
            with open(self.json2_path, "r", encoding="utf-8") as f:
                monitor_list = json.load(f)
            
        user_monitored_games = {}
        for game_id, game_info in monitor_list.items():
            if sender_id in game_info["user_id"]:
                user_monitored_games[game_id] = game_info

        if not user_monitored_games:
            yield event.plain_result("暂无已订阅游戏。")
            return
        
        message_parts = [Comp.Plain(text="您已订阅的游戏列表：\n")]
        for game_id, game_info in user_monitored_games.items():
            game_name = game_info.get('name', '未知游戏')
            last_price = game_info.get('last_price', '未初始化')
            original_price = game_info.get('original_price', 'N/A')
            discount = game_info.get('discount', 'N/A')
            
            message_parts.append(Comp.Plain(text=f"《{game_name}》 (AppID: {game_id})\n"))
            message_parts.append(Comp.Plain(text=f"  - 当前缓存价格：¥{last_price:.2f}" if isinstance(last_price, (int, float)) else f"  - 当前缓存价格：{last_price}\n"))
            message_parts.append(Comp.Plain(text=f"  - 原价：¥{original_price:.2f}" if isinstance(original_price, (int, float)) else f"  - 原价：{original_price}\n"))
            message_parts.append(Comp.Plain(text=f"  - 折扣：{discount}%" if isinstance(discount, (int, float)) else f"  - 折扣：{discount}\n"))
            message_parts.append(Comp.Plain(text=f"  - 链接：https://store.steampowered.com/app/{game_id}\n\n"))
            
        yield event.chain_result(message_parts)