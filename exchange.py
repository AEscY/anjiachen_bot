async def fetch_balance(self):
        """获取余额（极简安全版，打印原始结构以便调试）"""
        if not self.exchange:
            return {'USDT': {'free': 0}}
        try:
            raw = await self.exchange.fetch_balance()
            # 打印完整结构和各键的类型，帮助我们一次性定位问题
            logger.info(f"余额原始数据: {raw}")
            for k, v in raw.items():
                logger.info(f"  键: {k}, 类型: {type(v).__name__}, 值预览: {str(v)[:100]}")

            # 1. 优先使用 CCXT 标准字段 'total'
            total = raw.get('total')
            if isinstance(total, dict) and 'USDT' in total:
                return {'USDT': {'free': float(total['USDT'])}}

            # 2. 其次使用 'free'
            free = raw.get('free')
            if isinstance(free, dict) and 'USDT' in free:
                return {'USDT': {'free': float(free['USDT'])}}

            # 3. 尝试直接取 'USDT' 键
            usdt = raw.get('USDT')
            if isinstance(usdt, dict):
                return {'USDT': {'free': float(usdt.get('free', usdt.get('total', 0)))}}
            if isinstance(usdt, (int, float)):
                return {'USDT': {'free': float(usdt)}}

            # 4. 遍历所有键，只处理 dict 类型的值，且键包含 USDT
            for key, val in raw.items():
                if not isinstance(val, dict):
                    continue
                if 'USDT' in str(key).upper():
                    free_val = val.get('free', val.get('total', 0))
                    return {'USDT': {'free': float(free_val)}}

            # 如果以上都找不到，返回 0，并提示用户发送日志
            logger.error(f"无法解析余额，请将上面的日志发送给开发者")
        except Exception as e:
            logger.error(f"获取余额失败: {e}", exc_info=True)
        return {'USDT': {'free': 0}}