#!/usr/bin/env python3
"""
CFTC Positioning Replicator
============================
复制 JPM Delta-One Table 12: Traders in Financial Futures & COT Disaggregated

数据来源: CFTC Socrata API (免费, 无需API key)
输出: 单一HTML, 聚焦 Leveraged Funds (TFF) / Managed Money (Disagg)
     含多头/空头/净持仓的 position, z-score, w/w change

用法:
    python3 cftc_持仓分析.py              # 最新一期
    python3 cftc_持仓分析.py --date 2026-03-17  # 指定日期
"""

import pandas as pd
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timedelta
from html import escape
import sys
import warnings
import time
import os
warnings.filterwarnings('ignore')

# ============================================================================
# CONFIGURATION
# ============================================================================

CFTC_TFF_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_DISAGG_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
LOOKBACK_DAYS = 1200  # ~3.3年, 确保有足够历史数据
ZSCORE_WINDOW = 156   # 3年 = 156周

# ============================================================================
# CONTRACT MAPPINGS
# ============================================================================

# TFF: Equity, Fixed Income, Interest Rates, FX/Crypto
TFF_CONTRACTS = [
    # 股指
    {'name': '标普500',        'cftc': 'E-MINI S&P 500 -',   'section': '股指',   'yf': '^GSPC'},
    {'name': '纳斯达克100',    'cftc': 'NASDAQ MINI',          'section': '股指',   'yf': '^NDX'},
    {'name': '罗素2000',       'cftc': 'RUSSELL E-MINI',       'section': '股指',   'yf': '^RUT'},
    {'name': 'MSCI新兴市场',   'cftc': 'MSCI EM INDEX',        'section': '股指',   'yf': 'EEM'},
    {'name': 'MSCI发达市场',   'cftc': 'MSCI EAFE',            'section': '股指',   'yf': 'EFA'},
    {'name': '日经225',        'cftc': 'NIKKEI STOCK AVERAGE', 'section': '股指',   'yf': '^N225'},
    # 债券
    {'name': '2年期美债',           'cftc': 'UST 2Y NOTE',          'section': '债券', 'yf': 'ZT=F'},
    {'name': '10年期美债',          'cftc': 'UST 10Y NOTE',         'section': '债券', 'yf': 'ZN=F'},
    {'name': '超长期美债',          'cftc': 'ULTRA UST BOND',       'section': '债券', 'yf': 'UB=F'},
    # 利率
    {'name': '联邦基金',       'cftc': 'FED FUNDS',            'section': '利率',   'yf': 'ZQ=F'},
    # 外汇/加密
    {'name': '欧元/美元',  'cftc': 'EURO FX - CHICAGO',             'section': '外汇/加密', 'yf': 'EURUSD=X'},
    {'name': '英镑/美元',  'cftc': 'BRITISH POUND',                 'section': '外汇/加密', 'yf': 'GBPUSD=X'},
    {'name': '日元/美元',  'cftc': 'JAPANESE YEN',                  'section': '外汇/加密', 'yf': 'JPYUSD=X'},
    {'name': '澳元/美元',  'cftc': 'AUSTRALIAN DOLLAR',             'section': '外汇/加密', 'yf': 'AUDUSD=X'},
    {'name': '比特币',     'cftc': 'BITCOIN - CHICAGO MERCANTILE',  'section': '外汇/加密', 'yf': 'BTC-USD'},
]

DISAGG_CONTRACTS = [
    {'name': 'WTI原油',     'cftc': 'WTI-PHYSICAL',         'section': '能源',     'yf': 'CL=F'},
    {'name': '天然气',      'cftc': 'NAT GAS NYME',         'section': '能源',     'yf': 'NG=F'},
    {'name': '铜',          'cftc': 'COPPER- #1',           'section': '金属',     'yf': 'HG=F'},
    {'name': '黄金',        'cftc': 'GOLD - COMMODITY',     'section': '金属',     'yf': 'GC=F'},
    {'name': '白银',        'cftc': 'SILVER - COMMODITY',   'section': '金属',     'yf': 'SI=F'},
    {'name': '玉米',        'cftc': 'CORN - CHICAGO',       'section': '农产品',   'yf': 'ZC=F'},
]

# ============================================================================
# DATA FETCHING
# ============================================================================

def fetch_cftc(endpoint, start_date, limit=50000):
    """从CFTC Socrata API获取数据 (含重试)"""
    params = {
        "$where": f"report_date_as_yyyy_mm_dd >= '{start_date}'",
        "$limit": limit,
        "$order": "report_date_as_yyyy_mm_dd ASC"
    }
    for attempt in range(3):
        try:
            resp = requests.get(endpoint, params=params, timeout=120)
            resp.raise_for_status()
            break
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            if attempt < 2:
                print(f"    [RETRY {attempt+1}] Connection error, retrying in 3s...")
                time.sleep(3)
            else:
                raise

    df = pd.DataFrame(resp.json())
    if df.empty:
        return df

    skip_cols = {'market_and_exchange_names', 'report_date_as_yyyy_mm_dd',
                 'cftc_contract_market_code', 'cftc_market_code', 'cftc_commodity_code',
                 'cftc_region_code', 'cftc_subgroup_code', 'contract_market_name',
                 'contract_units', 'futonly_or_combined', 'id', 'commodity',
                 'commodity_group_name', 'commodity_name', 'commodity_subgroup_name',
                 'report_date_as_mm_dd_yyyy', 'yyyy_report_week_ww'}
    for col in df.columns:
        if col not in skip_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df['report_date'] = pd.to_datetime(df['report_date_as_yyyy_mm_dd'])
    return df


def match_cftc(df, search_pattern):
    """在CFTC数据中按名称匹配合约"""
    if search_pattern is None:
        return None
    names_upper = df['market_and_exchange_names'].str.upper()
    pattern_upper = search_pattern.upper()

    mask = names_upper == pattern_upper
    if not mask.any():
        mask = names_upper.str.startswith(pattern_upper, na=False)
    if not mask.any():
        mask = df['market_and_exchange_names'].str.contains(search_pattern, case=False, na=False)

    matched = df[mask].copy()
    if matched.empty:
        return None

    if matched['market_and_exchange_names'].nunique() > 1:
        names = matched['market_and_exchange_names'].unique()
        for n in names:
            if 'Consolidated' in n:
                matched = matched[matched['market_and_exchange_names'] == n]
                break
        else:
            avg_oi = matched.groupby('market_and_exchange_names')['open_interest_all'].mean()
            matched = matched[matched['market_and_exchange_names'] == avg_oi.idxmax()]

    # Deduplicate sub-contracts: keep only the one with highest avg OI per contract code
    if 'cftc_contract_market_code' in matched.columns and matched['cftc_contract_market_code'].nunique() > 1:
        avg_oi = matched.groupby('cftc_contract_market_code')['open_interest_all'].mean()
        matched = matched[matched['cftc_contract_market_code'] == avg_oi.idxmax()]

    return matched.sort_values('report_date').reset_index(drop=True)


# ============================================================================
# PROCESSING
# ============================================================================

def calc_zscore(series, window=ZSCORE_WINDOW):
    s = series.dropna()
    if len(s) < 10:
        return np.nan
    tail = s.tail(window)
    mean, std = tail.mean(), tail.std()
    if std == 0 or np.isnan(std):
        return 0.0
    return round((s.iloc[-1] - mean) / std, 1)


def calc_change_zscore(series, window=ZSCORE_WINDOW):
    changes = series.diff().dropna()
    if len(changes) < 10:
        return np.nan
    tail = changes.tail(window)
    mean, std = tail.mean(), tail.std()
    if std == 0 or np.isnan(std):
        return 0.0
    return round((changes.iloc[-1] - mean) / std, 1)


def _pos_group(matched, long_col, short_col):
    """计算一组持仓的 net/long/short 的 position, z-score, w/w change
    z-score 使用 OI 归一化后的占比（与 toggle chart 口径一致）"""
    long_s = matched[long_col].fillna(0)
    short_s = matched[short_col].fillna(0)
    net_s = long_s - short_s
    oi = matched['open_interest_all'].fillna(0).replace(0, np.nan)

    long_oi = long_s / oi
    short_oi = short_s / oi
    net_oi = net_s / oi

    latest_long = float(long_s.iloc[-1])
    latest_short = float(short_s.iloc[-1])
    latest_net = latest_long - latest_short

    z_dlong = calc_change_zscore(long_s)
    z_dshort = calc_change_zscore(short_s)
    result = {
        'net': int(latest_net),
        'net_z': calc_zscore(net_oi),
        'net_ww': int(net_s.diff().iloc[-1]) if len(net_s) > 1 else 0,
        'net_ww_z': calc_change_zscore(net_s),
        'long': int(latest_long),
        'long_z': calc_zscore(long_oi),
        'long_ww': int(long_s.diff().iloc[-1]) if len(long_s) > 1 else 0,
        'long_ww_z': z_dlong,
        'short': int(latest_short),
        'short_z': calc_zscore(short_oi),
        'short_ww': int(short_s.diff().iloc[-1]) if len(short_s) > 1 else 0,
        'short_ww_z': z_dshort,
        'flow_state': _flow_state(z_dlong, z_dshort),
    }
    return result


def _flow_state(z_dlong, z_dshort):
    """根据多空变化z-score判定flow state"""
    if z_dlong is None or z_dshort is None:
        return ''
    if isinstance(z_dlong, float) and np.isnan(z_dlong):
        return ''
    if isinstance(z_dshort, float) and np.isnan(z_dshort):
        return ''
    zl, zs = float(z_dlong), float(z_dshort)

    # 双向极端 (优先判定)
    if zl >= 0.8 and zs <= -0.8:
        return '多头挤压'
    if zl <= -0.8 and zs >= 0.8:
        return '空头施压'
    if zl >= 0.8 and zs >= 0.8:
        return '多空双增'
    if zl <= -0.8 and zs <= -0.8:
        return '多空双减'
    # 单向主导
    if zl >= 0.8 and abs(zs) < 0.5:
        return '多头建仓'
    if zs <= -0.8 and abs(zl) < 0.5:
        return '空头回补'
    if zs >= 0.8 and abs(zl) < 0.5:
        return '空头建仓'
    if zl <= -0.8 and abs(zs) < 0.5:
        return '多头平仓'
    return ''

import json
import urllib.request
import urllib.parse

def fetch_sina_market_kline(symbol, channel="us"):
    """
    通用免密行情直连引擎：
    彻底绕过 Yahoo Finance 频控拉黑限制，支持外汇、期货、美股/ETF、全球指数。
    """
    if channel == "futures":
        # 新浪全球商品期货日K (原油、黄金、白银、铜、天然气、玉米)
        url = f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{symbol}=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}&_={int(time.time()*1000)}"
    elif channel == "forex":
        # 新浪外汇/汇率日K (欧元、英镑、日元、澳元等)
        url = f"https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_{symbol}=/NewForexService.getDayKLine?symbol={symbol}&_={int(time.time()*1000)}"
    else:
        # 新浪美股/ETF/全球指数日K
        url = f"https://stock.finance.sina.com.cn/usstock/api/jsonp.php/IO.XSRV2.CallbackList['kline']/US_MinKService.getDailyK?symbol={symbol}&_={int(time.time()*1000)}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finance.sina.com.cn/"
    })

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
            start_idx = content.find('([')
            end_idx = content.rfind('])')
            if start_idx != -1 and end_idx != -1:
                json_str = content[start_idx + 1: end_idx + 1]
                data = json.loads(json_str)
                df = pd.DataFrame(data)
                if not df.empty:
                    # 兼容不同接口的字段名 (d: 日期, c: 收盘价)
                    date_col = 'd' if 'd' in df.columns else ('date' if 'date' in df.columns else None)
                    close_col = 'c' if 'c' in df.columns else ('close' if 'close' in df.columns else None)
                    if date_col and close_col:
                        df['date'] = pd.to_datetime(df[date_col])
                        df['close'] = pd.to_numeric(df[close_col], errors='coerce')
                        df = df.dropna(subset=['close']).sort_values('date')
                        return df.set_index('date')['close']
    except Exception:
        pass
    return None


def fetch_tue_tue_returns(contracts, cftc_date):
    """
    获取 CFTC 同期 Tue→Tue 价格变动
    全品种免密路由分发，彻底杜绝 429 Rate limited 报错
    """
    results = {}
    tue_end = pd.Timestamp(cftc_date)
    tue_start = tue_end - timedelta(days=7)

    # 21 个核心资产的全通道映射配置字典
    ROUTE_MAP = {
        # 股指
        '^GSPC':     {'sym': '.INX',     'channel': 'us'},       # 标普500
        '^NDX':      {'sym': '.IXIC',    'channel': 'us'},       # 纳斯达克 (代理纳指100)
        '^RUT':      {'sym': 'IWM',      'channel': 'us'},       # 罗素2000 ETF代理
        'EEM':       {'sym': 'EEM',      'channel': 'us'},       # MSCI新兴市场ETF
        'EFA':       {'sym': 'EFA',      'channel': 'us'},       # MSCI发达市场ETF
        '^N225':     {'sym': '.N225',    'channel': 'us'},       # 日经225指数

        # 债券与利率期货 (使用高流动性、高度拟合的基准美债ETF替代)
        'ZT=F':      {'sym': 'SHY',      'channel': 'us'},       # 2年期美债代理
        'ZN=F':      {'sym': 'IEF',      'channel': 'us'},       # 10年期美债代理
        'UB=F':      {'sym': 'TLT',      'channel': 'us'},       # 20+年超长期美债代理
        'ZQ=F':      {'sym': 'SHV',      'channel': 'us'},       # 短期利率/联邦基金代理

        # 外汇与加密
        'EURUSD=X':  {'sym': 'fx_seurusd', 'channel': 'forex'},    # 欧元/美元
        'GBPUSD=X':  {'sym': 'fx_sgbpusd', 'channel': 'forex'},    # 英镑/美元
        'JPYUSD=X':  {'sym': 'fx_sjpyusd', 'channel': 'forex'},    # 日元/美元
        'AUDUSD=X':  {'sym': 'fx_saudusd', 'channel': 'forex'},    # 澳元/美元
        'BTC-USD':   {'sym': 'BTCUSD',     'channel': 'us'},       # 比特币现货

        # 大宗商品期货
        'CL=F':      {'sym': 'hf_CL',    'channel': 'futures'},  # WTI原油
        'NG=F':      {'sym': 'hf_NG',    'channel': 'futures'},  # 天然气
        'HG=F':      {'sym': 'hf_HG',    'channel': 'futures'},  # 铜
        'GC=F':      {'sym': 'hf_GC',    'channel': 'futures'},  # 黄金
        'SI=F':      {'sym': 'hf_SI',    'channel': 'futures'},  # 白银
        'ZC=F':      {'sym': 'hf_C',     'channel': 'futures'},  # 玉米
    }

    for c in contracts:
        yf_ticker = c.get('yf')
        name = c['name']
        if not yf_ticker:
            continue

        route = ROUTE_MAP.get(yf_ticker)
        if not route:
            continue

        close_series = fetch_sina_market_kline(route['sym'], route['channel'])
        if close_series is not None and not close_series.empty:
            px_end = close_series[close_series.index <= tue_end]
            px_start = close_series[close_series.index <= tue_start]
            if len(px_end) > 0 and len(px_start) > 0:
                p1 = float(px_start.iloc[-1])
                p2 = float(px_end.iloc[-1])
                d1 = px_start.index[-1].strftime('%m/%d')
                d2 = px_end.index[-1].strftime('%m/%d')
                ret = (p2 / p1 - 1) * 100
                results[name] = {
                    'ret': round(ret, 2),
                    'ticker': yf_ticker,
                    'date_start': d1,
                    'date_end': d2,
                    'px_start': p1,
                    'px_end': p2,
                }

    return results


def build_table12_tff(df_tff, contracts, price_data=None):
    """构建TFF部分: 只保留 Leveraged Funds, 含多空分拆"""
    rows = []
    for c in contracts:
        matched = match_cftc(df_tff, c['cftc'])
        if matched is None or matched.empty:
            continue
        lf = _pos_group(matched, 'lev_money_positions_long', 'lev_money_positions_short')
        lf['Instrument'] = c['name']
        lf['_section'] = c['section']
        pd_info = price_data.get(c['name']) if price_data else None
        lf['price_chg'] = pd_info['ret'] if pd_info else None
        rows.append(lf)
    return pd.DataFrame(rows)


def build_table12_disagg(df_disagg, contracts, price_data=None):
    """构建Disagg部分: 只保留 Managed Money, 含多空分拆"""
    rows = []
    for c in contracts:
        matched = match_cftc(df_disagg, c['cftc'])
        if matched is None or matched.empty:
            continue
        mm = _pos_group(matched, 'm_money_positions_long_all', 'm_money_positions_short_all')
        mm['Instrument'] = c['name']
        mm['_section'] = c['section']
        pd_info = price_data.get(c['name']) if price_data else None
        mm['price_chg'] = pd_info['ret'] if pd_info else None
        rows.append(mm)
    return pd.DataFrame(rows)


# ============================================================================
# HTML OUTPUT
# ============================================================================

CSS = """
:root {
    --blue: #4472C4; --blue-light: #D6E4F0; --orange: #C55A11;
    --green-bg: #C6EFCE; --green-txt: #006100;
    --red-bg: #FFC7CE; --red-txt: #9C0006;
    --gray-border: #D9D9D9; --row-alt: #F8F9FA;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif; font-size: 13px;
       color: #333; background: #fff; padding: 20px 30px; max-width: 1800px; margin: 0 auto; }
header { border-bottom: 3px solid var(--orange); padding-bottom: 12px; margin-bottom: 24px;
         display: flex; justify-content: space-between; align-items: flex-end; }
header h1 { font-size: 22px; font-weight: 700; color: var(--orange); }
header .meta { font-size: 12px; color: #666; text-align: right; }

table { border-collapse: collapse; width: 100%; font-size: 12px; margin-bottom: 4px; }
thead th { background: var(--blue); color: #fff; font-weight: 600; font-size: 11px;
           padding: 7px 6px; text-align: center; border: 1px solid #3a62a0; white-space: nowrap; }
thead th:first-child { text-align: left; }
thead th.group-header { background: #3a62a0; border-bottom: 2px solid var(--orange); font-size: 12px; }
tbody td { padding: 4px 6px; border: 1px solid var(--gray-border); text-align: right; white-space: nowrap; }
tbody td:first-child { text-align: left; font-weight: 600; background: #FAFAFA; }
tbody tr:nth-child(even) { background: var(--row-alt); }
tbody tr:hover { background: #EBF0F7; }

.section-row td { background: var(--blue-light) !important; font-weight: 700; color: var(--blue);
                   padding: 6px 8px; font-size: 12px; }
.pos { color: var(--green-txt); } .neg { color: var(--red-txt); }
.pos-bg { background: var(--green-bg) !important; color: var(--green-txt); font-weight: 600; }
.neg-bg { background: var(--red-bg) !important; color: var(--red-txt); font-weight: 600; }

.zbar { position: relative; min-width: 50px; padding: 0 !important; text-align: center !important; overflow: hidden; }
.zbar-inner { position: absolute; top: 1px; bottom: 1px; opacity: 0.35; }
.zbar-pos { background: #00B050; left: 50%; } .zbar-neg { background: #FF0000; right: 50%; }
.zbar-label { position: relative; z-index: 1; font-size: 11px; font-weight: 600; padding: 4px 3px; display: block; }

.tag { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 10px; font-weight: 700; white-space: nowrap; }
.tag-bull { background: #C6EFCE; color: #006100; }
.tag-bear { background: #FFC7CE; color: #9C0006; }
.tag-mixed { background: #FFF2CC; color: #7F6000; }
.tag-crowded { background: #FCE4D6; color: #C55A11; }
.tag-vcrowded { background: #F4B084; color: #833C0B; }
.tag-extreme { background: #FF6347; color: #fff; }

.divergence { background: #FFF3CD !important; border: 2px solid #FFCA2C !important; font-weight: 700; }

.source { font-size: 10px; color: #999; margin-top: 4px; }
.notes { font-size: 11px; color: #666; margin-top: 20px; padding: 12px 16px;
         background: #F8F9FA; border-radius: 4px; border: 1px solid var(--gray-border); }
.notes ul { margin: 6px 0 0 18px; } .notes li { margin-bottom: 3px; }

@media print { body { padding: 10px; font-size: 11px; } }
"""


def _zbar(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return '<td class="zbar"><span class="zbar-label"></span></td>'
    v = float(val)
    pct = min(abs(v) / 3.0 * 50, 50)
    if v > 0:
        bar = f'<div class="zbar-inner zbar-pos" style="width:{pct:.0f}%"></div>'
        cls = 'pos'
    elif v < 0:
        bar = f'<div class="zbar-inner zbar-neg" style="width:{pct:.0f}%"></div>'
        cls = 'neg'
    else:
        bar, cls = '', ''
    return f'<td class="zbar">{bar}<span class="zbar-label {cls}">{v:.1f}</span></td>'


def _chg_td(chg, z):
    chg_s = f'{int(chg):,}' if chg is not None and not (isinstance(chg, float) and np.isnan(chg)) else ''
    z_s = f'{float(z):.1f}z' if z is not None and not (isinstance(z, float) and np.isnan(z)) else ''
    cls = 'pos' if chg and chg > 0 else ('neg' if chg and chg < 0 else '')
    display = f'{chg_s} ({z_s})' if z_s else chg_s
    return f'<td class="{cls}">{display}</td>'


def _num_td(val, large=True):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return '<td></td>'
    cls = 'pos' if val > 0 else ('neg' if val < 0 else '')
    s = f'{int(val):,}' if large else str(val)
    return f'<td class="{cls}">{s}</td>'


def _flow_tag(state):
    if not state:
        return '<td></td>'
    bull = {'多头建仓', '空头回补', '多头挤压'}
    bear = {'空头建仓', '多头平仓', '空头施压'}
    mixed = {'多空双增', '多空双减'}
    cls = 'tag-bull' if state in bull else ('tag-bear' if state in bear else 'tag-mixed')
    return f'<td><span class="tag {cls}">{escape(state)}</span></td>'


def _crowding_tag(net_z, long_z=None, short_z=None):
    """根据 net/long/short z-score 判定拥挤度 (与 toggle chart 口径一致)"""
    def _safe(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return 0.0
        return float(v)
    nz, lz, sz = _safe(net_z), _safe(long_z), _safe(short_z)
    if nz >= 2.0 or lz >= 2.0:
        label = '极端多头' if nz >= 2.75 or lz >= 2.75 else '拥挤多头'
        cls = 'tag-extreme' if '极端' in label else 'tag-crowded'
        return f'<td><span class="tag {cls}">{label}</span></td>'
    if nz <= -2.0 or sz >= 2.0:
        label = '极端空头' if nz <= -2.75 or sz >= 2.75 else '拥挤空头'
        cls = 'tag-extreme' if '极端' in label else 'tag-crowded'
        return f'<td><span class="tag {cls}">{label}</span></td>'
    return '<td></td>'


def _pct_td(val, divergence=False):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return '<td></td>'
    cls = 'pos' if val > 0 else ('neg' if val < 0 else '')
    if divergence:
        cls += ' divergence'
    return f'<td class="{cls}">{val:+.1f}%</td>'


def _is_divergence(flow_state, price_chg):
    """判断动作和价格是否背离"""
    if not flow_state or price_chg is None:
        return False
    if isinstance(price_chg, float) and np.isnan(price_chg):
        return False
    bull = {'多头建仓', '空头回补', '多头挤压'}
    bear = {'空头建仓', '多头平仓', '空头施压'}
    if flow_state in bull and price_chg < -0.05:
        return True
    if flow_state in bear and price_chg > 0.05:
        return True
    return False


def _row_html(r):
    cells = f'<td>{escape(str(r["Instrument"]))}</td>'
    divergence = _is_divergence(r.get('flow_state', ''), r.get('price_chg'))
    cells += _pct_td(r.get('price_chg'), divergence=divergence)
    cells += _num_td(r['net'])
    cells += _zbar(r['net_z'])
    cells += _chg_td(r['net_ww'], r['net_ww_z'])
    cells += _num_td(r['long'])
    cells += _zbar(r['long_z'])
    cells += _chg_td(r['long_ww'], r['long_ww_z'])
    cells += _num_td(r['short'])
    cells += _zbar(r['short_z'])
    cells += _chg_td(r['short_ww'], r['short_ww_z'])
    cells += _flow_tag(r.get('flow_state', ''))
    cells += _crowding_tag(r.get('net_z'), r.get('long_z'), r.get('short_z'))
    return f'<tr>{cells}</tr>'


def _price_detail_table(price_data):
    """生成价格验证明细表"""
    if not price_data:
        return ''
    rows = []
    for name, info in price_data.items():
        cls = 'pos' if info['ret'] > 0 else ('neg' if info['ret'] < 0 else '')
        # 根据价格大小决定小数位
        decimals = 2 if info['px_start'] >= 1 else 4
        rows.append(
            f'<tr><td>{escape(name)}</td>'
            f'<td style="color:#666">{escape(info["ticker"])}</td>'
            f'<td>{info["date_start"]}</td>'
            f'<td style="text-align:right">{info["px_start"]:,.{decimals}f}</td>'
            f'<td>{info["date_end"]}</td>'
            f'<td style="text-align:right">{info["px_end"]:,.{decimals}f}</td>'
            f'<td class="{cls}" style="text-align:right;font-weight:600">{info["ret"]:+.2f}%</td></tr>'
        )
    return f"""
    <br>
    <h3 style="color:var(--orange);margin-bottom:8px">同期涨跌验证明细 (Tue→Tue)</h3>
    <table>
        <thead><tr>
            <th>资产</th><th>Ticker</th><th>起始日</th><th>起始收盘</th><th>截止日</th><th>截止收盘</th><th>涨跌</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    <div class="source">数据来源: yfinance API | 取每个日期当天或之前最近交易日的收盘价</div>"""


def generate_html(df_tff, df_disagg, report_date, price_data=None):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    # TFF rows with section headers
    tff_rows = []
    last_section = None
    for _, r in df_tff.iterrows():
        sec = r.get('_section', '')
        if sec != last_section:
            last_section = sec
            tff_rows.append(f'<tr class="section-row"><td colspan="13">{escape(sec)}</td></tr>')
        tff_rows.append(_row_html(r))

    # Disagg rows with section headers
    disagg_rows = []
    last_section = None
    for _, r in df_disagg.iterrows():
        sec = r.get('_section', '')
        if sec != last_section:
            last_section = sec
            disagg_rows.append(f'<tr class="section-row"><td colspan="13">{escape(sec)}</td></tr>')
        disagg_rows.append(_row_html(r))

    sub_headers = """<tr>
        <th>净持仓</th><th>z</th><th>周变化</th>
        <th>多头</th><th>z</th><th>周变化</th>
        <th>空头</th><th>z</th><th>周变化</th>
    </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CFTC 持仓报告 - {escape(report_date)}</title>
    <style>{CSS}</style>
</head>
<body>
    <header>
        <h1>CFTC 期货持仓分析</h1>
        <div class="meta">数据截止 {escape(report_date)}<br>生成时间 {escape(now)}<br>数据来源: CFTC Socrata API + yfinance</div>
    </header>

    <h3 style="color:var(--orange);margin-bottom:8px">杠杆基金 Leveraged Funds（TFF 报告）</h3>
    <table>
        <thead>
            <tr>
                <th rowspan="2">资产</th>
                <th rowspan="2" class="group-header">同期涨跌</th>
                <th colspan="3" class="group-header">净持仓</th>
                <th colspan="3" class="group-header">多头</th>
                <th colspan="3" class="group-header">空头</th>
                <th rowspan="2" class="group-header">动作</th>
                <th rowspan="2" class="group-header">拥挤度</th>
            </tr>
            {sub_headers}
        </thead>
        <tbody>{''.join(tff_rows)}</tbody>
    </table>
    <div class="source">数据来源: CFTC Traders in Financial Futures</div>

    <br>

    <h3 style="color:var(--orange);margin-bottom:8px">管理资金 Managed Money（COT 分类报告）</h3>
    <table>
        <thead>
            <tr>
                <th rowspan="2">资产</th>
                <th rowspan="2" class="group-header">同期涨跌</th>
                <th colspan="3" class="group-header">净持仓</th>
                <th colspan="3" class="group-header">多头</th>
                <th colspan="3" class="group-header">空头</th>
                <th rowspan="2" class="group-header">动作</th>
                <th rowspan="2" class="group-header">拥挤度</th>
            </tr>
            {sub_headers}
        </thead>
        <tbody>{''.join(disagg_rows)}</tbody>
    </table>
    <div class="source">数据来源: CFTC Disaggregated COT</div>

    <div class="notes">
        <strong>说明</strong>
        <ul>
            <li>z-score = (当前值 - 156周均值) / 156周标准差（3年窗口）</li>
            <li>周变化 = 周环比合约数变化（括号内为该变化的z-score）</li>
            <li>净持仓 = 多头 - 空头 | 同期涨跌 = CFTC报告期 Tue→Tue 价格变动</li>
            <li>动作: 多头建仓/平仓、空头建仓/回补、多头挤压/空头施压、多空双增/双减</li>
            <li>拥挤度: net/long/short z 任一 ≥ 2.0 → 拥挤 | ≥ 2.75 → 极端</li>
            <li><span class="divergence" style="padding:1px 6px;font-size:10px">黄色高亮</span> = 动作与同期价格背离（如看多资金+价格下跌，或看空资金+价格上涨）</li>
            <li>MSCI新兴/发达市场同期涨跌使用EEM/EFA (ETF代理†)，MSCI指数本身在yfinance不可用</li>
        </ul>
    </div>

{_price_detail_table(price_data)}

</body>
</html>"""


# ============================================================================
# HELP DISPLAY
# ============================================================================

def print_help():
    help_text = """
=============================================================
CFTC 持仓分析工具 (CFTC Positioning Replicator)
=============================================================
复制 JPM Delta-One Table 12: Traders in Financial Futures & COT Disaggregated

数据来源: CFTC Socrata API (免费, 无需API key)
输出: 在当前目录的 `0_持仓报告` 文件夹下生成 HTML 报告。

用法 (Usage):
    python cftc_持仓分析.py              # 获取最新一期的持仓数据
    python cftc_持仓分析.py --date <日期> # 获取指定日期的持仓数据 (格式: YYYY-MM-DD)
    python cftc_持仓分析.py -h, --help   # 显示此帮助信息

示例 (Examples):
    python cftc_持仓分析.py
    python cftc_持仓分析.py --date 2026-03-17
=============================================================
"""
    print(help_text)

# ============================================================================
# MAIN
# ============================================================================

def main():
    # 检查是否传入帮助命令
    if '-h' in sys.argv or '--help' in sys.argv:
        print_help()
        sys.exit(0)

    target_date = None
    if '--date' in sys.argv:
        idx = sys.argv.index('--date')
        if idx + 1 < len(sys.argv):
            target_date = sys.argv[idx + 1]

    output_dir = "0_持仓报告"
    
    # 提前检查：如果指定了日期，且该日期的文件已存在于文件夹中，则直接跳过下载
    if target_date:
        expected_file = os.path.join(output_dir, f'cftc_持仓报告_{target_date}.html')
        if os.path.exists(expected_file):
            print(f"⏭️ 目标文件 {expected_file} 已存在，跳过下载与生成。")
            return

    start_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    print("=" * 50)
    print("CFTC Positioning Replicator")
    if target_date:
        print(f"  Target date: {target_date}")
    print("=" * 50)

    # 1. Fetch CFTC data
    print("\n[1/3] Fetching CFTC data...")
    print("  TFF...")
    df_tff = fetch_cftc(CFTC_TFF_URL, start_date)
    print(f"    -> {len(df_tff)} rows")

    print("  Disaggregated...")
    df_disagg = fetch_cftc(CFTC_DISAGG_URL, start_date)
    print(f"    -> {len(df_disagg)} rows")

    if target_date:
        cutoff = pd.Timestamp(target_date)
        df_tff = df_tff[df_tff['report_date'] <= cutoff]
        df_disagg = df_disagg[df_disagg['report_date'] <= cutoff]

    report_date = df_tff['report_date'].max().strftime('%Y-%m-%d') if not df_tff.empty else 'N/A'
    print(f"  Report date: {report_date}")

    # 二次检查：如果在没传 --date 跑最新数据的情况下发现文件已存在，也可在此拦截
    if not target_date and report_date != 'N/A':
        expected_file = os.path.join(output_dir, f'cftc_持仓报告_{report_date}.html')
        if os.path.exists(expected_file):
            print(f"⏭️ 最新报告 {expected_file} 已存在，跳过剩余步骤。")
            return

    # 2. Fetch price data (Tue→Tue 同期价格变动)
    print("\n[2/4] Fetching price data (Tue→Tue)...")
    all_contracts = TFF_CONTRACTS + DISAGG_CONTRACTS
    price_data = fetch_tue_tue_returns(all_contracts, report_date)
    print(f"  -> {len(price_data)}/{len([c for c in all_contracts if c.get('yf')])} instruments")

    # 3. Build tables
    print("\n[3/4] Building tables...")
    df_t12_tff = build_table12_tff(df_tff, TFF_CONTRACTS, price_data)
    df_t12_disagg = build_table12_disagg(df_disagg, DISAGG_CONTRACTS, price_data)
    print(f"  TFF: {len(df_t12_tff)} instruments | Disagg: {len(df_t12_disagg)} instruments")

    # 4. Write HTML
    print("\n[4/4] Writing HTML...")
    html = generate_html(df_t12_tff, df_t12_disagg, report_date, price_data)

    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f'cftc_持仓报告_{report_date}.html')

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  -> {output_file}")

    # Preview
    if not df_t12_tff.empty:
        print(f"\n--- Leveraged Funds Preview ---")
        cols = ['Instrument', 'net', 'net_z', 'net_ww', 'long', 'long_ww', 'short', 'short_ww']
        print(df_t12_tff[cols].head(8).to_string(index=False))


if __name__ == '__main__':
    main()