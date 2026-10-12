/**
 * 技术指标计算库 —— 纯函数
 *
 * 输入: bars = [{date, open, high, low, close, volume}, ...]
 * 输出: 与 bars 等长的数组 (不足周期处为 null), 便于直接喂给 ECharts series
 *
 * 支持: MA / EMA / BOLL / CCI / BIAS / SAR / ATR
 */
(function (global) {
  'use strict';

  function pick(bars, key) { return bars.map(function (b) { return b[key]; }); }

  /** 简单移动平均 */
  function sma(arr, n) {
    var out = new Array(arr.length).fill(null);
    var sum = 0;
    for (var i = 0; i < arr.length; i++) {
      sum += arr[i];
      if (i >= n) sum -= arr[i - n];
      if (i >= n - 1) out[i] = sum / n;
    }
    return out;
  }

  /** 指数移动平均 */
  function ema(arr, n) {
    var out = new Array(arr.length).fill(null);
    var k = 2 / (n + 1);
    var prev = null;
    for (var i = 0; i < arr.length; i++) {
      prev = prev === null ? arr[i] : arr[i] * k + prev * (1 - k);
      out[i] = i >= n - 1 ? prev : null;
    }
    return out;
  }

  /** 总体标准差 */
  function stddev(arr, n) {
    var mean = sma(arr, n);
    var out = new Array(arr.length).fill(null);
    for (var i = n - 1; i < arr.length; i++) {
      var m = mean[i], s = 0;
      for (var j = i - n + 1; j <= i; j++) s += Math.pow(arr[j] - m, 2);
      out[i] = Math.sqrt(s / n);
    }
    return out;
  }

  /** 布林带 BOLL(n, k) */
  function boll(bars, n, k) {
    n = n || 20; k = k || 2;
    var c = pick(bars, 'close');
    var mid = sma(c, n);
    var sd = stddev(c, n);
    var upper = new Array(c.length).fill(null);
    var lower = new Array(c.length).fill(null);
    for (var i = 0; i < c.length; i++) {
      if (mid[i] !== null) {
        upper[i] = mid[i] + k * sd[i];
        lower[i] = mid[i] - k * sd[i];
      }
    }
    return { mid: mid, upper: upper, lower: lower };
  }

  /** 顺势指标 CCI(n) */
  function cci(bars, n) {
    n = n || 14;
    var tp = bars.map(function (b) { return (b.high + b.low + b.close) / 3; });
    var ma = sma(tp, n);
    var out = new Array(tp.length).fill(null);
    for (var i = n - 1; i < tp.length; i++) {
      var md = 0;
      for (var j = i - n + 1; j <= i; j++) md += Math.abs(tp[j] - ma[i]);
      md /= n;
      out[i] = md === 0 ? 0 : (tp[i] - ma[i]) / (0.015 * md);
    }
    return out;
  }

  /** 乖离率 BIAS(n) = (close - MA(n)) / MA(n) * 100 */
  function bias(bars, n) {
    n = n || 6;
    var c = pick(bars, 'close');
    var ma = sma(c, n);
    return c.map(function (v, i) {
      return ma[i] ? (v - ma[i]) / ma[i] * 100 : null;
    });
  }

  /** 真实波幅 ATR(n) —— Wilder 平滑 */
  function atr(bars, n) {
    n = n || 14;
    var tr = bars.map(function (b, i) {
      if (i === 0) return b.high - b.low;
      var pc = bars[i - 1].close;
      return Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
    });
    var out = new Array(bars.length).fill(null);
    var prev = null;
    for (var i = 0; i < tr.length; i++) {
      if (i < n - 1) continue;
      if (prev === null) {
        var s = 0;
        for (var j = 0; j <= i; j++) s += tr[j];
        prev = s / n;
      } else {
        prev = (prev * (n - 1) + tr[i]) / n;
      }
      out[i] = prev;
    }
    return out;
  }

  /** 抛物线 SAR (af 加速因子, maxAf 上限) */
  function sar(bars, af, maxAf) {
    af = af || 0.02; maxAf = maxAf || 0.2;
    var n = bars.length;
    var out = new Array(n).fill(null);
    if (n < 2) return out;
    var isUp = bars[1].close >= bars[0].close;
    var ep = isUp ? bars[0].high : bars[0].low;
    var val = isUp ? bars[0].low : bars[0].high;
    var acc = af;
    out[0] = val;
    for (var i = 1; i < n; i++) {
      val = val + acc * (ep - val);
      var p1 = bars[i - 1], p2 = bars[i - 2] || bars[i - 1];
      if (isUp) {
        val = Math.min(val, p1.low, p2.low);
        if (bars[i].low < val) { isUp = false; val = ep; ep = bars[i].low; acc = af; }
        else if (bars[i].high > ep) { ep = bars[i].high; acc = Math.min(acc + af, maxAf); }
      } else {
        val = Math.max(val, p1.high, p2.high);
        if (bars[i].high > val) { isUp = true; val = ep; ep = bars[i].high; acc = af; }
        else if (bars[i].low < ep) { ep = bars[i].low; acc = Math.min(acc + af, maxAf); }
      }
      out[i] = val;
    }
    return out;
  }

  /** 根据勾选的指标, 生成 ECharts series (叠加在主图上) */
  function overlaySeries(bars, enabled) {
    var series = [];
    enabled = enabled || {};
    var c = pick(bars, 'close');

    if (enabled.ma) {
      [[5, '#ffaa00'], [10, '#00b5ff'], [20, '#c678dd'], [60, '#2ed573']].forEach(function (p) {
        series.push({
          name: 'MA' + p[0], type: 'line', data: sma(c, p[0]),
          lineStyle: { width: 1, color: p[1] }, itemStyle: { color: p[1] },
          showSymbol: false, smooth: true, xAxisIndex: 0, yAxisIndex: 0,
        });
      });
    }
    if (enabled.boll) {
      var b = boll(bars, 20, 2);
      [['BOLL上轨', b.upper, '#ff6b81'], ['BOLL中轨', b.mid, '#e4e7ed'], ['BOLL下轨', b.lower, '#2ed573']].forEach(function (p) {
        series.push({
          name: p[0], type: 'line', data: p[2],
          lineStyle: { width: 1, color: p[2], type: p[0] === 'BOLL中轨' ? 'dashed' : 'solid' },
          itemStyle: { color: p[2] }, showSymbol: false, xAxisIndex: 0, yAxisIndex: 0,
        });
      });
    }
    if (enabled.sar) {
      series.push({
        name: 'SAR', type: 'scatter', data: sar(bars), symbolSize: 4,
        itemStyle: { color: '#00b5ff' }, xAxisIndex: 0, yAxisIndex: 0,
      });
    }
    if (enabled.cci) {
      series.push({
        name: 'CCI', type: 'line', data: cci(bars, 14), showSymbol: false,
        lineStyle: { width: 1, color: '#e6a23c' }, itemStyle: { color: '#e6a23c' },
        xAxisIndex: 2, yAxisIndex: 2,
      });
    }
    if (enabled.bias) {
      series.push({
        name: 'BIAS', type: 'line', data: bias(bars, 6), showSymbol: false,
        lineStyle: { width: 1, color: '#c678dd' }, itemStyle: { color: '#c678dd' },
        xAxisIndex: 2, yAxisIndex: 2,
      });
    }
    if (enabled.atr) {
      series.push({
        name: 'ATR', type: 'line', data: atr(bars, 14), showSymbol: false,
        lineStyle: { width: 1, color: '#ffaa00' }, itemStyle: { color: '#ffaa00' },
        xAxisIndex: 2, yAxisIndex: 2,
      });
    }
    return series;
  }

  /** MACD(12,26,9): DIF = EMA12 - EMA26, DEA = EMA(DIF,9), 柱 = (DIF-DEA)*2 */
  function macd(bars, fast, slow, signal) {
    fast = fast || 12; slow = slow || 26; signal = signal || 9;
    var c = pick(bars, 'close');
    var ef = ema(c, fast), es = ema(c, slow);
    var dif = c.map(function (v, i) {
      return (ef[i] !== null && es[i] !== null) ? ef[i] - es[i] : null;
    });
    var dea = ema(dif, signal);
    var hist = dif.map(function (v, i) {
      return (v !== null && dea[i] !== null) ? (v - dea[i]) * 2 : null;
    });
    return { dif: dif, dea: dea, hist: hist };
  }

  global.Indicators = {
    sma: sma, ema: ema, boll: boll, cci: cci, bias: bias, sar: sar, atr: atr, macd: macd,
    overlaySeries: overlaySeries,
  };
})(window);
