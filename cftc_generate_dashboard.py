import os
import glob
import re
import json
import sys
import time
import requests
import pandas as pd
import akshare as ak
import yfinance as yf
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings('ignore')

# ================= 配置区 =================
# ★ 报告源文件 / 缓存 / 数据导出 统一放在 report 文件夹
REPORT_DIR = "report"
os.makedirs(REPORT_DIR, exist_ok=True)

# ★ 生成的分析面板输出到【当前文件夹】（与脚本同级）
OUTPUT_FILE = "cftc_dashboard.html"

# 持仓报告 HTML 命名规则：report/cftc_持仓报告_YYYY-MM-DD.html
HTML_PATTERN = os.path.join(REPORT_DIR, "cftc_持仓报告_*.html")
# 供前端日历点击时拼接 URL 使用的相对前缀（必须以 / 结尾）
REPORT_URL_PREFIX = REPORT_DIR + "/"
REPORT_FILE_STEM = "cftc_持仓报告_"
REPORT_FILE_EXT = ".html"

PRICE_CACHE_FILE = os.path.join(REPORT_DIR, "cftc_价格历史缓存.json")
DATA_EXPORT_FILE = os.path.join(REPORT_DIR, "cftc_面板完整数据.json")

# 针对不同资产配置最优的数据源映射策略
ASSET_CONFIG = {
    # 宏观利率 (真实收益率 %，配置 ETF 降级保护)
    '2年期美债': {'type': 'us_yield', 'column': '美国国债收益率2年', 'fallback_etf': 'SHY', 'desc': '2年期美债基准收益率'},
    '10年期美债': {'type': 'us_yield', 'column': '美国国债收益率10年', 'fallback_etf': 'IEF', 'desc': '10年期美债基准收益率'},
    '超长期美债': {'type': 'us_yield', 'column': '美国国债收益率30年', 'fallback_etf': 'TLT', 'desc': '超长期美债收益率/TLT代理'},
    
    # 外盘商品期货 (原味期货美元报价)
    '黄金': {'type': 'futures', 'symbol': 'GC', 'desc': '纽约黄金主力连续期货'},
    '白银': {'type': 'futures', 'symbol': 'SI', 'desc': '纽约白银主力连续期货'},
    '铜': {'type': 'futures', 'symbol': 'HG', 'desc': 'COMEX精铜连续期货'},
    'WTI原油': {'type': 'futures', 'symbol': 'CL', 'desc': 'WTI原油主力连续期货'},
    '天然气': {'type': 'futures', 'symbol': 'NG', 'desc': '纽约天然气连续期货'},
    '玉米': {'type': 'futures', 'symbol': 'C', 'desc': 'CBOT玉米连续期货'},
    
    # 核心股指 (原生指数点数)
    '标普500': {'type': 'index_sina', 'symbol': '.INX', 'fallback_etf': 'SPY', 'desc': '标普500原生指数'},
    '纳斯达克100': {'type': 'index_sina', 'symbol': '.NDX', 'fallback_etf': 'QQQ', 'desc': '纳斯达克100原生指数'},
    '日经225': {'type': 'yf_index', 'symbol': '^N225', 'fallback_etf': 'EWJ', 'desc': '日经225原生指数 (^N225)'},
    
    # MSCI 官方原生代码
    'MSCI发达市场': {'type': 'yf_asset', 'symbol': 'MSCIWOR', 'fallback_etf': 'EFA', 'desc': 'MSCI发达市场 (MSCIWOR)'},
    'MSCI新兴市场': {'type': 'yf_asset', 'symbol': 'MSCIEF', 'fallback_etf': 'EEM', 'desc': 'MSCI新兴市场 (MSCIEF)'},
    
    # 外汇原生汇率指数
    '欧元/美元': {'type': 'yf_asset', 'symbol': 'EURUSD=X', 'fallback_etf': 'FXE', 'desc': 'EUR/USD 原生汇率指数'},
    '英镑/美元': {'type': 'yf_asset', 'symbol': 'GBPUSD=X', 'fallback_etf': 'FXB', 'desc': 'GBP/USD 原生汇率指数'},
    '日元/美元': {'type': 'yf_asset', 'symbol': 'JPYUSD=X', 'fallback_etf': 'FXY', 'desc': 'JPY/USD 原生汇率指数'},
    '澳元/美元': {'type': 'yf_asset', 'symbol': 'AUDUSD=X', 'fallback_etf': 'FXA', 'desc': 'AUD/USD 原生汇率指数'},
    
    # 比特币现货：多级火箭拉取策略 (Binance -> CoinGecko -> YF)
    '比特币': {'type': 'custom_api', 'api_source': 'crypto_multi', 'symbol': 'BTC', 'desc': '真实比特币现货 (多源保障)'},

    # 其他纯宽基 (使用高流动性 ETF 代理)
    '罗素2000': {'type': 'etf_proxy', 'symbol': 'IWM', 'desc': 'IWM ETF 代理'},
    '联邦基金': {'type': 'etf_proxy', 'symbol': 'BIL', 'desc': 'BIL 短债基准代理'}
}
# ==========================================

# ================= 缓存系统 =================
def load_cache():
    if os.path.exists(PRICE_CACHE_FILE):
        try:
            with open(PRICE_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache):
    try:
        with open(PRICE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存缓存文件失败: {e}")
# ==========================================

def clean_number(text):
    if not text: return 0
    cleaned = re.sub(r'[^\d\-]', '', text)
    try:
        return int(cleaned) if cleaned and cleaned != '-' else 0
    except ValueError:
        return 0

def parse_html_file(filepath):
    """解析生成的单期持仓 HTML 报告"""
    match = re.search(r'\d{4}-\d{2}-\d{2}', filepath)
    if not match: return []
    date_str = match.group()
    
    data_list = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')
            for row in soup.find_all('tr'):
                # 排除表头行和 section 导航行
                if row.find('th') or row.get('class') == ['section-row']:
                    continue
                cols = row.find_all('td')
                if len(cols) >= 10 and not cols[0].has_attr('colspan'):
                    asset_name = cols[0].text.strip()
                    # 匹配资产并提取持仓数据
                    if asset_name in ASSET_CONFIG:
                        data_list.append({
                            'Date': date_str,
                            'Asset': asset_name,
                            'Net': clean_number(cols[2].text),
                            'Long': clean_number(cols[5].text),
                            'Short': clean_number(cols[8].text)
                        })
    except Exception as e:
        pass
    return data_list

def enrich_with_prices(df):
    print("\n🌐 开始匹配历史资产走势...")
    df['Price'] = None
    assets = df['Asset'].unique()
    
    min_date = df['Date'].min() - pd.Timedelta(days=7)
    max_date = df['Date'].max() + pd.Timedelta(days=7)
    
    cache = load_cache()
    yield_data_cache = None
    yield_fetched = False

    for asset in assets:
        if asset not in ASSET_CONFIG:
            continue
            
        cfg = ASSET_CONFIG[asset]
        desc = cfg.get('desc', '数据拉取') 
        
        mask = df['Asset'] == asset
        asset_dates = df.loc[mask, 'Date']
        if asset_dates.empty: continue
        
        max_req_date = asset_dates.max().strftime('%Y-%m-%d')
        asset_cache = cache.get(asset, {})
        
        need_fetch = True
        if asset_cache:
            max_cached_date = max(asset_cache.keys())
            if max_req_date <= max_cached_date:
                need_fetch = False
                
        if need_fetch:
            sys.stdout.write(f"\r📈 [网络拉取] {asset} ({desc}) ...       ")
            sys.stdout.flush()
            close_px = pd.Series(dtype=float)
            
            try:
                # --- 策略 A: 自定义直连多级 API ---
                if cfg['type'] == 'custom_api':
                    if cfg['api_source'] == 'crypto_multi':
                        success = False
                        # 1级火箭: Binance
                        try:
                            url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1000"
                            resp = requests.get(url, timeout=5).json()
                            if isinstance(resp, list) and len(resp) > 0:
                                tmp_df = pd.DataFrame(resp, columns=['date', 'open', 'high', 'low', 'close', 'vol', 'close_time', 'qav', 'num_trades', 'tbbav', 'tbqav', 'ignore'])
                                tmp_df['date'] = pd.to_datetime(tmp_df['date'], unit='ms').dt.normalize()
                                tmp_df.set_index('date', inplace=True)
                                close_px = tmp_df['close'].astype(float).dropna()
                                success = True
                                sys.stdout.write(f" [通过 Binance API 命中] ")
                        except Exception: pass
                        
                        # 2级火箭: CoinGecko
                        if not success or close_px.empty:
                            try:
                                url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1000"
                                resp = requests.get(url, timeout=5).json()
                                if 'prices' in resp:
                                    tmp_df = pd.DataFrame(resp['prices'], columns=['date', 'close'])
                                    tmp_df['date'] = pd.to_datetime(tmp_df['date'], unit='ms').dt.normalize()
                                    tmp_df.set_index('date', inplace=True)
                                    close_px = tmp_df['close'].astype(float).dropna()
                                    close_px = close_px[~close_px.index.duplicated(keep='last')]
                                    success = True
                                    sys.stdout.write(f" [通过 CoinGecko API 命中] ")
                            except Exception: pass

                        # 3级火箭: Yahoo Finance 兜底
                        if not success or close_px.empty:
                            try:
                                ticker_obj = yf.Ticker('BTC-USD')
                                hist = ticker_obj.history(start=min_date.strftime('%Y-%m-%d'), end=max_date.strftime('%Y-%m-%d'))
                                if not hist.empty and 'Close' in hist.columns:
                                    if hist.index.tz is not None: hist.index = hist.index.tz_localize(None)
                                    close_px = hist['Close'].astype(float).dropna()
                                    sys.stdout.write(f" [通过 YF API 兜底命中] ")
                            except Exception: pass

                # --- 策略 B: 国债收益率 ---
                elif cfg['type'] == 'us_yield':
                    if not yield_fetched:
                        try:
                            yield_data_cache = ak.bond_zh_us_rate(start_date="20200101")
                            yield_data_cache['日期'] = pd.to_datetime(yield_data_cache['日期'])
                            yield_data_cache.set_index('日期', inplace=True)
                        except Exception: pass
                        yield_fetched = True
                        
                    if yield_data_cache is not None:
                        col = cfg['column']
                        if col in yield_data_cache.columns:
                            close_px = yield_data_cache[col].dropna()

                # --- 策略 C: 商品期货 ---
                elif cfg['type'] == 'futures':
                    hist = ak.futures_foreign_hist(symbol=cfg['symbol'])
                    if not hist.empty:
                        hist.columns = [str(c).lower() for c in hist.columns]
                        if 'date' in hist.columns and 'close' in hist.columns:
                            hist['date'] = pd.to_datetime(hist['date'])
                            hist.set_index('date', inplace=True)
                            close_px = hist['close'].astype(float).dropna()

                # --- 策略 D: 新浪美股指数 ---
                elif cfg['type'] == 'index_sina':
                    hist = ak.index_us_stock_sina(symbol=cfg['symbol'])
                    if not hist.empty:
                        hist.columns = [str(c).lower() for c in hist.columns]
                        if 'date' in hist.columns and 'close' in hist.columns:
                            hist['date'] = pd.to_datetime(hist['date'])
                            hist.set_index('date', inplace=True)
                            close_px = hist['close'].astype(float).dropna()

                # --- 策略 E: Yahoo Finance 原生资源 ---
                elif cfg['type'] in ['yf_asset', 'yf_index']:
                    try:
                        ticker_obj = yf.Ticker(cfg['symbol'])
                        hist = ticker_obj.history(start=min_date.strftime('%Y-%m-%d'), end=max_date.strftime('%Y-%m-%d'))
                        if not hist.empty and 'Close' in hist.columns:
                            if hist.index.tz is not None: hist.index = hist.index.tz_localize(None)
                            close_px = hist['Close'].astype(float).dropna()
                    except Exception: pass
                
                # --- 策略 F: ETF 代理 ---
                elif cfg['type'] == 'etf_proxy':
                    hist = ak.stock_us_daily(symbol=cfg['symbol'], adjust="qfq")
                    if not hist.empty:
                        hist.columns = [str(c).lower() for c in hist.columns]
                        if 'date' in hist.columns and 'close' in hist.columns:
                            hist['date'] = pd.to_datetime(hist['date'])
                            hist.set_index('date', inplace=True)
                            close_px = hist['close'].astype(float).dropna()

                # --- 防断连终极降级保护 (支持国债 ETF 降级如 TLT/IEF) ---
                if (close_px.empty or len(close_px) == 0) and cfg.get('fallback_etf'):
                    sys.stdout.write(f" [降级拉取对应 ETF: {cfg['fallback_etf']}] ")
                    sys.stdout.flush()
                    try:
                        hist = ak.stock_us_daily(symbol=cfg['fallback_etf'], adjust="qfq")
                        if not hist.empty:
                            hist.columns = [str(c).lower() for c in hist.columns]
                            if 'date' in hist.columns and 'close' in hist.columns:
                                hist['date'] = pd.to_datetime(hist['date'])
                                hist.set_index('date', inplace=True)
                                close_px = hist['close'].astype(float).dropna()
                    except Exception:
                        pass

                # 存入缓存
                if not close_px.empty:
                    if close_px.index.tz is not None:
                        close_px.index = close_px.index.tz_localize(None)
                    
                    daily_dict = {d.strftime('%Y-%m-%d'): float(v) for d, v in close_px.items() if pd.notna(v)}
                    cache[asset] = {**asset_cache, **daily_dict}
                    save_cache(cache)
                    asset_cache = cache[asset]
                    
                time.sleep(0.5) 
                
            except Exception as e:
                print(f"\n⚠️ {asset} 网络拉取异常: {e}")
                
        else:
            sys.stdout.write(f"\r⚡ [命中缓存] {asset} (极速加载) ...       ")
            sys.stdout.flush()

        # ================= 日期对齐 =================
        if asset_cache:
            cached_series = pd.Series(asset_cache)
            cached_series.index = pd.to_datetime(cached_series.index)
            cached_series = cached_series.sort_index()
            
            prices = []
            for d in asset_dates:
                available_dates = cached_series[cached_series.index <= d]
                if not available_dates.empty:
                    prices.append(float(available_dates.iloc[-1]))
                else:
                    prices.append(None)
                    
            df.loc[mask, 'Price'] = prices

    print(f"\n✅ 数据准备完毕！价格数据已自动持久化至: {PRICE_CACHE_FILE}")
    return df

# 修改后:
def generate_dashboard(df):
    df = df.sort_values(['Asset', 'Date'])
    
    # 按照 ASSET_CONFIG 中定义的自然顺序排列（确保 10年期美债紧跟超长期美债）
    all_unique = df['Asset'].unique().tolist()
    config_order = list(ASSET_CONFIG.keys())
    assets = [a for a in config_order if a in all_unique] + [a for a in all_unique if a not in config_order]
    
    full_data = {}
    for asset in assets:
        if asset not in ASSET_CONFIG:
            continue
            
        sub = df[df['Asset'] == asset]
        safe_prices = [float(p) if pd.notna(p) else None for p in sub['Price']]
        
        full_data[asset] = {
            'dates': sub['Date'].dt.strftime('%Y-%m-%d').tolist(),
            'longs': sub['Long'].tolist(),
            'shorts': sub['Short'].tolist(),
            'nets': sub['Net'].tolist(),
            'prices': safe_prices,
            'config': ASSET_CONFIG[asset]
        }

    try:
        with open(DATA_EXPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(full_data, f, ensure_ascii=False, indent=4)
        print(f"💾 面板完整数据源已导出至: {DATA_EXPORT_FILE}")
    except Exception:
        pass

    latest_date = df['Date'].max().strftime('%Y-%m-%d')

    # ★ 收集所有报告日期（供前端日历高亮 & 点击跳转）
    report_dates = sorted(df['Date'].dt.strftime('%Y-%m-%d').unique().tolist())

    html_template = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>CFTC 全维度智能量价面板</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
    <style>
        body {{ display: flex; height: 100vh; margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: #f0f2f5; }}
        #sidebar {{ width: 280px; background: #fff; border-right: 1px solid #ddd; display: flex; flex-direction: column; }}
        .search-box {{ padding: 15px; border-bottom: 1px solid #eee; background: #fafafa; }}
        #assetSearch {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; outline: none; font-size: 14px; transition: border-color 0.2s; }}
        #assetSearch:focus {{ border-color: #1890ff; }}
        #assetList {{ flex: 1; overflow-y: auto; padding: 10px; }}
        .asset-btn {{ width: 100%; text-align: left; padding: 12px 15px; margin-bottom: 6px; border: none; background: transparent; cursor: pointer; border-radius: 6px; font-size: 14px; font-weight: 500; transition: all 0.2s; border-left: 4px solid transparent;}}
        .asset-btn:hover {{ background: #e6f7ff; color: #1890ff; }}
        .asset-btn.active {{ background: #e6f7ff; color: #1890ff; border-left: 4px solid #1890ff; font-weight: bold; }}
        .home-btn {{ font-weight: bold !important; border-bottom: 1px dashed #eee; border-radius: 0 !important; margin-bottom: 0 !important; }}

        /* ============ 日历组件 ============ */
        .calendar-container {{
            padding: 12px 12px 14px 12px;
            border-bottom: 1px solid #eee;
            background: #fafafa;
            flex-shrink: 0;
        }}
        .calendar-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }}
        .calendar-header .cal-title {{
            font-size: 13px;
            font-weight: 700;
            color: #333;
            user-select: none;
        }}
        .calendar-header button {{
            background: #fff;
            border: 1px solid #ddd;
            border-radius: 4px;
            width: 24px;
            height: 24px;
            cursor: pointer;
            font-size: 13px;
            color: #666;
            line-height: 1;
            padding: 0;
            transition: all 0.15s;
        }}
        .calendar-header button:hover {{
            background: #e6f7ff;
            color: #1890ff;
            border-color: #1890ff;
        }}
        .calendar-header .cal-actions {{
            display: flex;
            gap: 4px;
        }}
        .calendar-weekdays {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 2px;
            margin-bottom: 4px;
        }}
        .calendar-weekdays span {{
            text-align: center;
            font-size: 11px;
            color: #999;
            padding: 2px 0;
            user-select: none;
        }}
        .calendar-grid {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 2px;
        }}
        .calendar-cell {{
            text-align: center;
            font-size: 11px;
            padding: 5px 0;
            border-radius: 4px;
            color: #ccc;
            user-select: none;
            transition: all 0.15s;
            min-height: 20px;
            line-height: 1.2;
        }}
        .calendar-cell.empty {{
            cursor: default;
        }}
        .calendar-cell.no-report {{
            cursor: default;
            color: #cfcfcf;
        }}
        .calendar-cell.has-report {{
            background: #e6f7ff;
            color: #1890ff;
            font-weight: 700;
            cursor: pointer;
            border: 1px solid #91d5ff;
        }}
        .calendar-cell.has-report:hover {{
            background: #1890ff;
            color: #fff;
            border-color: #1890ff;
            transform: scale(1.12);
            box-shadow: 0 2px 6px rgba(24, 144, 255, 0.4);
        }}
        .calendar-cell.today {{
            outline: 2px solid #fa8c16;
            outline-offset: -2px;
        }}
        .calendar-legend {{
            margin-top: 8px;
            display: flex;
            gap: 10px;
            font-size: 10px;
            color: #999;
            justify-content: center;
        }}
        .calendar-legend .dot {{
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 2px;
            margin-right: 3px;
            vertical-align: middle;
        }}
        .calendar-legend .dot.blue {{ background: #e6f7ff; border: 1px solid #91d5ff; }}
        .calendar-legend .dot.gray {{ background: #f5f5f5; border: 1px solid #e0e0e0; }}

        #main {{ flex: 1; display: flex; flex-direction: column; padding: 20px; overflow: hidden; }}
        header {{ margin-bottom: 20px; background: #fff; padding: 20px 25px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
        h1 {{ margin: 0 0 8px 0; font-size: 22px; color: #1a1a1a; display: flex; align-items: center; gap: 10px; }}
        .badge-type {{ font-size: 12px; padding: 4px 8px; border-radius: 4px; font-weight: normal; }}
        .type-yield {{ background: #fff1f0; color: #f5222d; border: 1px solid #ffa39e; }}
        .type-futures {{ background: #f6ffed; color: #52c41a; border: 1px solid #b7eb8f; }}
        .type-proxy {{ background: #e6f7ff; color: #1890ff; border: 1px solid #91d5ff; }}
        .type-index {{ background: #f9f0ff; color: #722ed1; border: 1px solid #d3adf7; }}
        .type-yf {{ background: #fffbe6; color: #fa8c16; border: 1px solid #ffe58f; }}
        .type-custom {{ background: #fff0f6; color: #eb2f96; border: 1px solid #ffadd2; }}
        .info {{ color: #666; font-size: 14px; display: flex; flex-direction: column; gap: 5px; }}
        .sub-info {{ font-size: 13px; color: #888; }}
        #chart-container {{ flex: 1; background: #fff; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); padding: 20px; min-height: 400px; }}
        
        /* ============ 主页（数据总览）样式 ============ */
        #homeView {{ display: none; flex: 1; overflow: hidden; flex-direction: column; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 15px; }}
        .stat-card {{ background: #fff; padding: 18px 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); border-left: 4px solid #1890ff; transition: transform 0.15s; }}
        .stat-card:hover {{ transform: translateY(-2px); }}
        .stat-card .label {{ color: #888; font-size: 13px; margin-bottom: 8px; }}
        .stat-card .value {{ color: #1a1a1a; font-size: 24px; font-weight: 700; }}
        .stat-card.c1 {{ border-left-color: #1890ff; }}
        .stat-card.c2 {{ border-left-color: #722ed1; }}
        .stat-card.c3 {{ border-left-color: #52c41a; }}
        .stat-card.c4 {{ border-left-color: #fa8c16; }}
        .summary-container {{ flex: 1; background: #fff; border-radius: 10px; padding: 20px 25px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); overflow: auto; }}
        .summary-title {{ font-size: 16px; font-weight: 600; color: #1a1a1a; margin-bottom: 15px; display: flex; align-items: center; gap: 10px; }}
        .summary-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        .summary-table th {{ position: sticky; top: 0; background: #fafafa; padding: 12px; text-align: left; border-bottom: 2px solid #eee; font-weight: 600; color: #555; z-index: 1; }}
        .summary-table td {{ padding: 11px 12px; border-bottom: 1px solid #f0f0f0; }}
        .summary-table tbody tr {{ transition: background 0.15s; cursor: pointer; }}
        .summary-table tbody tr:hover {{ background: #f5faff; }}
        .pos {{ color: #d62728; font-weight: 600; }}
        .neg {{ color: #2ca02c; font-weight: 600; }}
        .neutral {{ color: #999; }}
    </style>
</head>
<body>
    <div id="sidebar">
        <div class="search-box">
            <input type="text" id="assetSearch" placeholder="🔍 搜索资产 (如: 超长期美债, 10年期)">
        </div>
        <div style="padding: 10px 10px 0 10px;">
            <button class="asset-btn home-btn active" onclick="showHome()">数据总览</button>
        </div>

        <!-- ★ 新增：报告日历 -->
        <div class="calendar-container">
            <div class="calendar-header">
                <button type="button" onclick="calPrevMonth()" title="上一月">‹</button>
                <span class="cal-title" id="calMonthLabel">—</span>
                <div class="cal-actions">
                    <button type="button" onclick="calToday()" title="回到最新报告月">⊙</button>
                    <button type="button" onclick="calNextMonth()" title="下一月">›</button>
                </div>
            </div>
            <div class="calendar-weekdays">
                <span>一</span><span>二</span><span>三</span><span>四</span>
                <span>五</span><span>六</span><span>日</span>
            </div>
            <div class="calendar-grid" id="calendarGrid"></div>
            <div class="calendar-legend">
                <span><span class="dot blue"></span>有报告</span>
                <span><span class="dot gray"></span>无报告</span>
            </div>
        </div>

        <div id="assetList"></div>
    </div>
    <div id="main">
        <header>
            <h1>
                <span id="currentAsset">📊 全资产数据总览</span>
                <span id="typeBadge" class="badge-type"></span>
            </h1>
            <div class="info">
                <span>统计区间: {df['Date'].min().strftime('%Y-%m-%d')} 至 {latest_date}</span>
                <span class="sub-info" id="dataSourceDesc">点击左侧资产列表，或点击下方表格行，查看单资产的量价对冲详细分析</span>
            </div>
        </header>

        <!-- ============ 主页 ============ -->
        <div id="homeView">
            <div class="stats-grid">
                <div class="stat-card c1">
                    <div class="label">追踪资产数量</div>
                    <div class="value" id="statAssets">-</div>
                </div>
                <div class="stat-card c2">
                    <div class="label">报告期数</div>
                    <div class="value" id="statPeriods">-</div>
                </div>
                <div class="stat-card c3">
                    <div class="label">数据起始日</div>
                    <div class="value" id="statStart" style="font-size: 18px;">-</div>
                </div>
                <div class="stat-card c4">
                    <div class="label">最新报告日</div>
                    <div class="value" id="statEnd" style="font-size: 18px;">-</div>
                </div>
            </div>
            <div class="summary-container">
                <div class="summary-title">📋 全部资产最新持仓概览 <span style="font-size:12px;color:#999;font-weight:normal;">（点击任意行查看详情）</span></div>
                <table class="summary-table">
                    <thead>
                        <tr>
                            <th>资产名称</th>
                            <th>最新日期</th>
                            <th>净持仓 (手)</th>
                            <th>环比变化</th>
                            <th>多头 (手)</th>
                            <th>空头 (手)</th>
                            <th>最新价格</th>
                        </tr>
                    </thead>
                    <tbody id="summaryBody"></tbody>
                </table>
            </div>
        </div>

        <!-- ============ 单资产图表 ============ -->
        <div id="chart-container" style="display:none;">
            <div id="chart" style="width: 100%; height: 100%;"></div>
        </div>
    </div>

    <script>
        const rawData = {json.dumps(full_data)};
        const assetList = Object.keys(rawData);
        let myChart = null;

        // ★ 报告日期列表（由 Python 侧注入）
        const reportDates = {json.dumps(report_dates)};
        // ★ 报告 HTML 的 URL 前缀与命名模板（由 Python 侧注入）
        const REPORT_URL_PREFIX = {json.dumps(REPORT_URL_PREFIX)};
        const REPORT_FILE_STEM  = {json.dumps(REPORT_FILE_STEM)};
        const REPORT_FILE_EXT   = {json.dumps(REPORT_FILE_EXT)};
        const reportDateSet = new Set(reportDates);

        // ============ 日历组件 ============
        let calYear = 0;
        let calMonth = 0;   // 0 ~ 11

        function calInit() {{
            if (reportDates.length > 0) {{
                const latest = new Date(reportDates[reportDates.length - 1] + "T00:00:00");
                calYear  = latest.getFullYear();
                calMonth = latest.getMonth();
            }} else {{
                const now = new Date();
                calYear  = now.getFullYear();
                calMonth = now.getMonth();
            }}
            calRender();
        }}

        function calRender() {{
            const label = document.getElementById('calMonthLabel');
            const grid  = document.getElementById('calendarGrid');
            if (!label || !grid) return;

            label.innerText = calYear + "年" + (calMonth + 1) + "月";
            grid.innerHTML = '';

            const firstDay = new Date(calYear, calMonth, 1);
            const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();

            // 周一为第一列：getDay() 周日=0 → 转成 周一=0
            let startWeekday = firstDay.getDay() - 1;
            if (startWeekday < 0) startWeekday = 6;

            // 上月补位
            for (let i = 0; i < startWeekday; i++) {{
                const cell = document.createElement('div');
                cell.className = 'calendar-cell empty';
                grid.appendChild(cell);
            }}

            const todayStr = (() => {{
                const t = new Date();
                const mm = String(t.getMonth() + 1).padStart(2, '0');
                const dd = String(t.getDate()).padStart(2, '0');
                return t.getFullYear() + '-' + mm + '-' + dd;
            }})();

            for (let d = 1; d <= daysInMonth; d++) {{
                const mm = String(calMonth + 1).padStart(2, '0');
                const dd = String(d).padStart(2, '0');
                const dateStr = calYear + '-' + mm + '-' + dd;

                const cell = document.createElement('div');
                cell.className = 'calendar-cell';
                cell.innerText = d;

                if (dateStr === todayStr) cell.classList.add('today');

                if (reportDateSet.has(dateStr)) {{
                    cell.classList.add('has-report');
                    cell.title = "查看 " + dateStr + " 的 CFTC 持仓报告";
                    cell.onclick = () => {{
                        const url = REPORT_URL_PREFIX + REPORT_FILE_STEM + dateStr + REPORT_FILE_EXT;
                        window.location.href = url;
                    }};
                }} else {{
                    cell.classList.add('no-report');
                }}

                grid.appendChild(cell);
            }}
        }}

        function calPrevMonth() {{
            calMonth--;
            if (calMonth < 0) {{ calMonth = 11; calYear--; }}
            calRender();
        }}

        function calNextMonth() {{
            calMonth++;
            if (calMonth > 11) {{ calMonth = 0; calYear++; }}
            calRender();
        }}

        function calToday() {{
            if (reportDates.length > 0) {{
                const latest = new Date(reportDates[reportDates.length - 1] + "T00:00:00");
                calYear  = latest.getFullYear();
                calMonth = latest.getMonth();
            }} else {{
                const now = new Date();
                calYear  = now.getFullYear();
                calMonth = now.getMonth();
            }}
            calRender();
        }}

        window.addEventListener('resize', () => {{ if (myChart) myChart.resize(); }});

        function getChart() {{
            if (!myChart) {{
                myChart = echarts.init(document.getElementById('chart'));
            }}
            return myChart;
        }}

        // ============ 侧边栏渲染 ============
        function renderAssetList(filter = '') {{
            const container = document.getElementById('assetList');
            container.innerHTML = '';
            assetList.filter(a => a.toLowerCase().includes(filter.toLowerCase())).forEach(asset => {{
                const btn = document.createElement('button');
                btn.className = 'asset-btn';
                btn.innerText = asset;
                btn.onclick = () => selectAsset(asset, btn);
                container.appendChild(btn);
            }});
        }}

        // ============ 主页切换 ============
        function showHome() {{
            document.querySelectorAll('.asset-btn').forEach(b => b.classList.remove('active'));
            const homeBtn = document.querySelector('.home-btn');
            if (homeBtn) homeBtn.classList.add('active');

            document.getElementById('homeView').style.display = 'flex';
            document.getElementById('chart-container').style.display = 'none';

            document.getElementById('currentAsset').innerText = '📊 全资产数据总览';
            const badge = document.getElementById('typeBadge');
            badge.className = '';
            badge.innerText = '';
            document.getElementById('dataSourceDesc').innerText = '点击左侧资产列表，或点击下方表格行，查看单资产的量价对冲详细分析';
        }}

        // ============ 价格格式化 ============
        function formatPrice(price, asset, cfg) {{
            if (price === null || price === undefined) return '<span style="color:#bbb;">未获取</span>';
            let decimals = 2;
            if (cfg.type === 'us_yield') decimals = 3;
            else if (asset.includes('/') || asset.includes('汇率')) decimals = 4;
            return Number(price).toLocaleString(undefined, {{minimumFractionDigits: decimals, maximumFractionDigits: decimals}});
        }}

        // ============ 构建主页统计与汇总表 ============
        function buildHomeView() {{
            const allDates = new Set();
            let assetCount = 0;
            for (const asset in rawData) {{
                const d = rawData[asset];
                d.dates.forEach(x => allDates.add(x));
                if (d.dates.length > 0) assetCount++;
            }}
            const sortedDates = [...allDates].sort();
            document.getElementById('statAssets').innerText = assetCount + ' 个';
            document.getElementById('statPeriods').innerText = sortedDates.length + ' 期';
            document.getElementById('statStart').innerText = sortedDates[0] || '—';
            document.getElementById('statEnd').innerText = sortedDates[sortedDates.length - 1] || '—';

            const tbody = document.getElementById('summaryBody');
            tbody.innerHTML = '';

            for (const asset in rawData) {{
                const d = rawData[asset];
                const n = d.dates.length;
                if (n === 0) continue;

                const net = d.nets[n - 1];
                const prevNet = n > 1 ? d.nets[n - 2] : null;
                const change = prevNet !== null ? net - prevNet : null;
                const price = d.prices[n - 1];

                // 净持仓
                const netHtml = net >= 0
                    ? '<span class="pos">' + net.toLocaleString() + '</span>'
                    : '<span class="neg">' + net.toLocaleString() + '</span>';

                // 环比变化（中国习惯：红涨绿跌）
                let changeHtml = '<span class="neutral">—</span>';
                if (change !== null) {{
                    if (change > 0) changeHtml = '<span class="pos">+' + change.toLocaleString() + '</span>';
                    else if (change < 0) changeHtml = '<span class="neg">' + change.toLocaleString() + '</span>';
                    else changeHtml = '<span class="neutral">0</span>';
                }}

                const tr = document.createElement('tr');
                tr.innerHTML =
                    '<td><strong>' + asset + '</strong></td>' +
                    '<td>' + d.dates[n - 1] + '</td>' +
                    '<td>' + netHtml + '</td>' +
                    '<td>' + changeHtml + '</td>' +
                    '<td>' + d.longs[n - 1].toLocaleString() + '</td>' +
                    '<td>' + d.shorts[n - 1].toLocaleString() + '</td>' +
                    '<td>' + formatPrice(price, asset, d.config) + '</td>';
                tr.onclick = () => {{
                    const btns = Array.from(document.querySelectorAll('.asset-btn'));
                    const target = btns.find(b => b.innerText === asset);
                    selectAsset(asset, target);
                }};
                tbody.appendChild(tr);
            }}
        }}

        // ============ 单资产详情 ============
        function selectAsset(name, btnElement) {{
            document.querySelectorAll('.asset-btn').forEach(b => b.classList.remove('active'));
            if (btnElement) btnElement.classList.add('active');
            const homeBtn = document.querySelector('.home-btn');
            if (homeBtn) homeBtn.classList.remove('active');

            document.getElementById('homeView').style.display = 'none';
            document.getElementById('chart-container').style.display = 'block';

            document.getElementById('currentAsset').innerText = name + " - 量价对冲分析";

            const data = rawData[name];
            const cfg = data.config;

            const badge = document.getElementById('typeBadge');
            const desc = document.getElementById('dataSourceDesc');
            let priceAxisName = '资产价格';
            let tooltipUnit = '';

            if (cfg.type === 'us_yield') {{
                badge.className = 'badge-type type-yield';
                badge.innerText = '宏观收益率曲线';
                desc.innerHTML = '⚠️ <strong>债市法则：</strong>国债期货持仓(多头做多价格) 与 收益率(紫线) 呈 <strong>反向关系</strong>。';
                priceAxisName = '收益率 (%)';
                tooltipUnit = '%';
            }} else if (cfg.type === 'futures') {{
                badge.className = 'badge-type type-futures';
                badge.innerText = '外盘商品期货';
                desc.innerHTML = '💡 <strong>数据说明：</strong>紫线为海外官方主力连续合约美元报价。';
                priceAxisName = '期货报价 ($)';
            }} else if (cfg.type === 'index_sina' || cfg.type === 'yf_index') {{
                badge.className = 'badge-type type-index';
                badge.innerText = '原生指数走势';
                desc.innerHTML = '💡 <strong>数据说明：</strong>直接获取官方核心指数的绝对点数。';
                priceAxisName = '指数点数';
            }} else if (cfg.type === 'custom_api') {{
                badge.className = 'badge-type type-custom';
                badge.innerText = name.includes('比特币') ? '原生现货直连' : 'API直连指数';
                desc.innerHTML = '💡 <strong>极客多源：</strong>内置多级防封禁架构，强制拉取纯净行情。';
                priceAxisName = name.includes('比特币') ? '现货报价 ($)' : '指数点数';
            }} else if (cfg.type === 'yf_asset') {{
                badge.className = 'badge-type type-yf';
                badge.innerText = name.includes('MSCI') ? '原生指数走势' : '原生汇率指数';
                desc.innerHTML = '💡 <strong>数据说明：</strong>精准对接 <strong>' + cfg.symbol + '</strong> 原生行情走势。';
                priceAxisName = name.includes('MSCI') ? '指数点数' : '汇率指数';
            }} else if (cfg.type === 'etf_proxy') {{
                badge.className = 'badge-type type-proxy';
                badge.innerText = '指数 ETF 穿透代理';
                desc.innerHTML = '💡 <strong>数据说明：</strong>因官方指数闭源收费，使用全球最大流动性跟踪 ETF (<strong>' + cfg.symbol + '</strong>) 进行走势完美拟合。';
                priceAxisName = 'ETF 净值 ($)';
            }}

            const hasPrice = data.prices.some(p => p !== null);

            const option = {{
                tooltip: {{ 
                    trigger: 'axis', 
                    axisPointer: {{ type: 'cross', crossStyle: {{ color: '#999' }} }},
                    backgroundColor: 'rgba(255, 255, 255, 0.95)',
                    borderColor: '#ccc',
                    borderWidth: 1,
                    textStyle: {{ color: '#333' }},
                    formatter: function (params) {{
                        let html = '<div style="font-weight:bold;margin-bottom:8px;border-bottom:1px solid #eee;padding-bottom:5px;">' + params[0].name + '</div>';
                        params.forEach(param => {{
                            let val = param.value;
                            if (param.seriesIndex === 3 && val != null) {{
                                let decimals = 2;
                                if (cfg.type === 'us_yield') decimals = 3;
                                else if (name.includes('/') || name.includes('汇率')) decimals = 4;
                                else if (name.includes('比特币')) decimals = 2;

                                val = Number(val).toLocaleString(undefined, {{
                                    minimumFractionDigits: decimals, 
                                    maximumFractionDigits: decimals
                                }}) + tooltipUnit;
                            }} else if (val != null) {{
                                val = Number(val).toLocaleString() + ' 手';
                            }}

                            html += '<div style="display:flex;justify-content:space-between;min-width:240px;margin:4px 0;">' +
                                    '<span>' + param.marker + param.seriesName + '</span>' + 
                                    '<span style="font-weight:bold; margin-left:15px;">' + (val == null ? '未获取' : val) + '</span>' +
                                    '</div>';
                        }});
                        return html;
                    }}
                }},
                legend: {{ data: ['多头 (Long)', '空头 (Short)', '净持仓 (Net)', priceAxisName], top: 5 }},
                grid: {{ left: '4%', right: '5%', bottom: '10%', top: '15%', containLabel: true }},
                dataZoom: [
                    {{ type: 'slider', start: 0, end: 100, bottom: 0, height: 25 }}, 
                    {{ type: 'inside' }}
                ],
                xAxis: {{ type: 'category', data: data.dates, boundaryGap: true, axisTick: {{ alignWithLabel: true }} }},
                yAxis: [
                    {{ 
                        type: 'value', 
                        name: '机构持仓量 (手)', 
                        position: 'left',
                        alignTicks: true,
                        splitLine: {{ lineStyle: {{ type: 'dashed', color: '#eee' }} }},
                        axisLabel: {{ formatter: (value) => value.toLocaleString() }}
                    }},
                    {{ 
                        type: 'value', 
                        name: hasPrice ? priceAxisName : '无数据', 
                        position: 'right',
                        alignTicks: true,
                        splitLine: {{ show: false }},
                        scale: true 
                    }}
                ],
                series: [
                    {{ name: '多头 (Long)', type: 'line', yAxisIndex: 0, data: data.longs, itemStyle: {{color: '#d62728'}}, smooth: true, showSymbol: false, lineStyle: {{width: 2.5}} }},
                    {{ name: '空头 (Short)', type: 'line', yAxisIndex: 0, data: data.shorts, itemStyle: {{color: '#2ca02c'}}, smooth: true, showSymbol: false, lineStyle: {{width: 2.5}} }},
                    {{ name: '净持仓 (Net)', type: 'bar', yAxisIndex: 0, data: data.nets, barMaxWidth: 40, itemStyle: {{ color: (p) => p.data >= 0 ? 'rgba(68, 114, 196, 0.45)' : 'rgba(255, 127, 14, 0.45)' }}, label: {{ show: false }} }},
                    {{ 
                        name: priceAxisName, 
                        type: 'line', 
                        yAxisIndex: 1, 
                        data: data.prices, 
                        itemStyle: {{color: '#8A2BE2'}}, 
                        smooth: true, 
                        showSymbol: true, 
                        symbolSize: 7,
                        lineStyle: {{width: 3, type: 'solid', shadowColor: 'rgba(138, 43, 226, 0.3)', shadowBlur: 8}},
                        connectNulls: true
                    }}
                ]
            }};
            const chart = getChart();
            chart.resize();
            chart.setOption(option, true);
        }}

        document.getElementById('assetSearch').oninput = (e) => renderAssetList(e.target.value);

        // ============ 初始化 ============
        renderAssetList();
        buildHomeView();
        showHome();
        calInit();       // ★ 渲染日历
    </script>
</body>
</html>
    """
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(html_template)
    print(f"🎉 完美收工！前端分析面板已生成: {OUTPUT_FILE}")

def main():
    files = sorted(glob.glob(HTML_PATTERN))
    if not files:
        print(f"❌ 未找到 HTML 文件，请检查 {REPORT_DIR} 目录。")
        return
        
    all_data = []
    try:
        for i, f in enumerate(files, 1):
            sys.stdout.write(f"\r⏳ 解析本地报告: [{i}/{len(files)}] {os.path.basename(f)}\n")
            sys.stdout.flush()
            all_data.extend(parse_html_file(f))
    except KeyboardInterrupt:
        print("\n🛑 解析中断，处理已读取的数据...")
    
    if all_data:
        df = pd.DataFrame(all_data)
        df['Date'] = pd.to_datetime(df['Date'])
        
        df = enrich_with_prices(df)
        generate_dashboard(df)
    else:
        print("\n❌ 未提取到任何有效数据。")

if __name__ == "__main__":
    main()