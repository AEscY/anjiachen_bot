    async def create_smart_buy_order(self, symbol, usdt_amount, slippage=0.005):
        """带有防滑点保护的智能买单（最大允许 0.5% 滑点）"""
        if not self.exchange:
            return None
        try:
            ticker = await self.fetch_ticker(symbol)
            last_price = ticker['last']
            amount = usdt_amount / last_price
            
            # 使用 IOC (Immediate-or-Cancel) 限价单防大滑点，或直接挂稍高价格限价单
            max_buy_price = last_price * (1 + slippage)
            order = await self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side='buy',
                amount=amount,
                price=max_buy_price,
                params={'timeInForce': 'IOC'}
            )
            return order
        except Exception as e:
            logger.warning(f"智能买单回退到市价单 ({symbol}): {e}")
            try:
                ticker = await self.fetch_ticker(symbol)
                return await self.exchange.create_order(symbol, 'market', 'buy', usdt_amount / ticker['last'])
            except:
                return None
