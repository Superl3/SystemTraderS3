import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

# Print friendly installation message if yfinance is missing
try:
    import yfinance as yf
except ImportError:
    print("\n[오류] 'yfinance' 라이브러리가 설치되어 있지 않습니다.")
    print("데이터를 다운로드하려면 먼저 아래 명령어로 라이브러리를 설치해 주세요:")
    print(">>> pip install yfinance")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("\n[오류] 'pandas' 라이브러리가 설치되어 있지 않습니다.")
    print("아래 명령어로 라이브러리를 설치해 주세요:")
    print(">>> pip install pandas")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance로부터 역사적 데이터를 다운로드하여 시뮬레이션 데이터셋을 구축합니다.")
    parser.add_argument(
        "--symbols", 
        default="005930.KS,000660.KS,035420.KS", 
        help="쉼표(,)로 구분된 주식 티커 목록 (예: AAPL,MSFT,GOOG 또는 005930.KS,000660.KS)"
    )
    parser.add_argument(
        "--benchmark", 
        default="^KS11", 
        help="벤치마크 지수 티커 (예: S&P500은 ^GSPC, 코스피는 ^KS11)"
    )
    parser.add_argument(
        "--start", 
        default="2020-01-01", 
        help="시작일 (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end", 
        default="2025-12-31", 
        help="종료일 (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--output-dir", 
        default="tests/fixtures/historical_data", 
        help="데이터셋을 저장할 디렉터리 경로"
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    benchmark = args.benchmark.strip()
    start_date = args.start
    end_date = args.end
    output_path = Path(args.output-dir)

    print(f"\n=========================================")
    print(f"      시뮬레이션 데이터 다운로더         ")
    print(f"=========================================")
    print(f"다운로드 대상 종목: {symbols}")
    print(f"벤치마크 지수    : {benchmark}")
    print(f"조회 기간       : {start_date} ~ {end_date}")
    print(f"저장 경로       : {output_path.resolve()}\n")

    # 디렉터리 생성
    output_path.mkdir(parents=True, exist_ok=True)

    # 1. 개별 종목 데이터 다운로드
    print("--- 1. 종목 가격 데이터 다운로드 중 ---")
    all_prices = []
    
    for symbol in symbols:
        print(f"다운로드 중: {symbol}...")
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start_date, end=end_date)
            if df.empty:
                print(f"  [경고] {symbol} 데이터가 비어 있습니다. 티커명을 확인해 주세요.")
                continue
                
            # 시간 형식 맞추기 (ISO 8601: YYYY-MM-DD)
            df = df.reset_index()
            # yfinance는 Date 컬럼에 타임스탬프 또는 날짜를 넣음
            df['timestamp'] = df['Date'].dt.strftime('%Y-%m-%d')
            df['symbol'] = symbol
            # Close(종가) 가격 추출
            df['price'] = df['Close'].round(2)
            
            # 저장 파일 생성 (AAA_prices.csv 형태)
            # 파일명에 특수문자 제거
            safe_symbol = "".join(c for c in symbol if c.isalnum() or c in "-_")
            symbol_file = output_path / f"{safe_symbol}_prices.csv"
            
            df_out = df[['timestamp', 'symbol', 'price']]
            df_out.to_csv(symbol_file, index=False, encoding='utf-8')
            print(f"  -> 완료: {symbol_file.name} ({len(df_out)}개 행)")
            
            all_prices.append(df)
        except Exception as e:
            print(f"  [오류] {symbol} 다운로드 실패: {e}")

    if not all_prices:
        print("[실패] 가격 데이터를 다운로드한 종목이 없습니다. 스크립트를 종료합니다.")
        return

    # 2. 벤치마크 데이터 다운로드
    if benchmark:
        print("\n--- 2. 벤치마크 지수 데이터 다운로드 중 ---")
        print(f"다운로드 중: {benchmark}...")
        try:
            ticker = yf.Ticker(benchmark)
            df_bench = ticker.history(start=start_date, end=end_date)
            if not df_bench.empty:
                df_bench = df_bench.reset_index()
                df_bench['timestamp'] = df_bench['Date'].dt.strftime('%Y-%m-%d')
                df_bench['symbol'] = benchmark
                df_bench['price'] = df_bench['Close'].round(2)
                
                bench_file = output_path / "benchmark_prices.csv"
                df_bench_out = df_bench[['timestamp', 'symbol', 'price']]
                df_bench_out.to_csv(bench_file, index=False, encoding='utf-8')
                print(f"  -> 완료: {bench_file.name} ({len(df_bench_out)}개 행)")
            else:
                print(f"  [경고] 벤치마크 {benchmark} 데이터가 비어 있습니다.")
        except Exception as e:
            print(f"  [오류] 벤치마크 다운로드 실패: {e}")

    # 3. 팩터 데이터 생성 (예: 20일 모멘텀 팩터)
    print("\n--- 3. 팩터(모멘텀) 데이터 생성 중 ---")
    factors_list = []
    
    for df in all_prices:
        symbol = df['symbol'].iloc[0]
        # 20일 종가 등락률 계산하여 단순 모멘텀 팩터 산출
        df['momentum'] = df['Close'].pct_change(periods=20).round(6)
        
        # NaN 제거
        df_factors = df.dropna(subset=['momentum'])
        
        for _, row in df_factors.iterrows():
            factors_list.append({
                'timestamp': row['timestamp'],
                'symbol': symbol,
                'factor_name': 'momentum',
                'factor_value': row['momentum']
            })
            
    if factors_list:
        df_factors_out = pd.DataFrame(factors_list)
        # 정렬
        df_factors_out = df_factors_out.sort_values(by=['timestamp', 'symbol'])
        factors_file = output_path / "factors.csv"
        df_factors_out.to_csv(factors_file, index=False, encoding='utf-8')
        print(f"  -> 완료: {factors_file.name} ({len(df_factors_out)}개 행)")
    else:
        print("  -> 생성할 수 있는 팩터 데이터가 없습니다. (데이터 부족)")

    print(f"\n[성공] 시뮬레이션용 데이터셋 구축이 완료되었습니다!")
    print(f"이제 대시보드 새로고침 시 '{output_path.name}' 데이터셋이 자동으로 추가되어 선택할 수 있습니다.")

if __name__ == "__main__":
    main()
