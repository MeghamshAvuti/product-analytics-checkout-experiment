import pandas as pd
import numpy as np
import os


# =====================================================
# LOAD DATA
# =====================================================

def load_data():

    print("Loading raw datasets...")

    users = pd.read_csv("raw_data/users.csv")
    sessions = pd.read_csv("raw_data/sessions.csv")
    events = pd.read_csv("raw_data/events.csv")
    orders = pd.read_csv("raw_data/orders.csv")
    order_items = pd.read_csv("raw_data/order_items.csv")
    campaigns = pd.read_csv("raw_data/campaigns.csv")
    products = pd.read_json("raw_data/products.json")

    print("Datasets loaded successfully.")

    return users, sessions, events, orders, order_items, campaigns, products


# =====================================================
# CLEAN SESSIONS
# =====================================================

def clean_sessions(sessions):

    print("Cleaning sessions data...")

    sessions = sessions.drop_duplicates(subset="session_id").copy()

    text_cols = ["device", "channel", "variant"]
    for col in text_cols:
        if col in sessions.columns:
            sessions[col] = sessions[col].astype(str).str.lower().str.strip()

    return sessions


# =====================================================
# BUILD FACT SESSIONS
# =====================================================

def build_fact_sessions(sessions, events, orders):

    print("Building fact_sessions...")

    events["event_ts"] = pd.to_datetime(events["event_ts"])
    sessions["session_start_ts"] = pd.to_datetime(sessions["session_start_ts"])

    events = events.sort_values(["session_id", "event_ts"])

    # First event timestamp per type
    event_first = (
        events.groupby(["session_id", "event_type"])["event_ts"]
        .min()
        .reset_index()
    )

    event_pivot = event_first.pivot(
        index="session_id",
        columns="event_type",
        values="event_ts"
    ).reset_index()

    fact_sessions = sessions.merge(event_pivot, on="session_id", how="left")

    # Funnel flags
    fact_sessions["has_product_view"] = fact_sessions["product_view"].notna()
    fact_sessions["has_add_to_cart"] = fact_sessions["add_to_cart"].notna()
    fact_sessions["has_begin_checkout"] = fact_sessions["begin_checkout"].notna()
    fact_sessions["has_payment_attempt"] = fact_sessions["payment_attempt"].notna()
    fact_sessions["has_purchase"] = fact_sessions["purchase"].notna()

    # Session duration
    last_event_time = (
        fact_sessions[
            ["purchase", "payment_attempt", "begin_checkout",
             "add_to_cart", "product_view"]
        ]
        .bfill(axis=1)
        .iloc[:, 0]
    )

    fact_sessions["session_duration_sec"] = (
        last_event_time - fact_sessions["session_start_ts"]
    ).dt.total_seconds()

    # Time-to metrics
    fact_sessions["time_to_cart_sec"] = (
        fact_sessions["add_to_cart"] - fact_sessions["session_start_ts"]
    ).dt.total_seconds()

    fact_sessions["time_to_checkout_sec"] = (
        fact_sessions["begin_checkout"] - fact_sessions["session_start_ts"]
    ).dt.total_seconds()

    fact_sessions["time_to_purchase_sec"] = (
        fact_sessions["purchase"] - fact_sessions["session_start_ts"]
    ).dt.total_seconds()

    # Revenue per session
    order_summary = orders.groupby("session_id").agg(
        gross_amount=("gross_amount", "sum"),
        discount_amount=("discount_amount", "sum"),
        net_amount=("net_amount", "sum")
    ).reset_index()

    fact_sessions = fact_sessions.merge(order_summary, on="session_id", how="left")

    fact_sessions[["gross_amount", "discount_amount", "net_amount"]] = \
        fact_sessions[["gross_amount", "discount_amount", "net_amount"]].fillna(0)
    
        # Rename for PDF compliance
    fact_sessions = fact_sessions.rename(
        columns={"session_start_ts": "start_ts"}
    )

    print("fact_sessions created.")
    return fact_sessions


# =====================================================
# BUILD FACT ORDERS (ENRICHED + OUTLIER HANDLING)
# =====================================================

def build_fact_orders(orders, order_items, products):

    print("Building fact_orders...")

    # -----------------------------
    # Merge product metadata
    # -----------------------------
    order_items_enriched = order_items.merge(
        products,
        on="product_id",
        how="left"
    )

    basket_summary = (
        order_items_enriched
        .groupby("order_id")
        .agg(
            total_items=("quantity", "sum"),
            distinct_products=("product_id", "nunique"),
            top_category=("category", lambda x: x.value_counts().idxmax()),
            category_mix=("category", lambda x: str(x.value_counts().to_dict())),
            average_rating=("rating", "mean"),
            total_cost=("cost", "sum")
        )
        .reset_index()
    )

    fact_orders = orders.merge(basket_summary, on="order_id", how="left")

    # -----------------------------
    # Margin proxy
    # -----------------------------
    fact_orders["margin_proxy"] = (
        fact_orders["net_amount"] - fact_orders["total_cost"]
    )

    # =====================================================
    # OUTLIER HANDLING (Required by PDF)
    # =====================================================

    # Log transform (for distribution analysis)
    fact_orders["log_net_amount"] = np.log1p(fact_orders["net_amount"])

    # IQR method for outlier detection
    Q1 = fact_orders["net_amount"].quantile(0.25)
    Q3 = fact_orders["net_amount"].quantile(0.75)
    IQR = Q3 - Q1

    lower_bound = Q1 - 1.5 * IQR
    upper_bound = Q3 + 1.5 * IQR

    # Flag outliers
    fact_orders["is_revenue_outlier"] = (
        (fact_orders["net_amount"] < lower_bound) |
        (fact_orders["net_amount"] > upper_bound)
    )

    # Cap revenue (winsorization)
    fact_orders["net_amount_capped"] = fact_orders["net_amount"].clip(
        lower=lower_bound,
        upper=upper_bound
    )

    print("fact_orders created with outlier handling.")
    return fact_orders


# =====================================================
# BUILD DIM USERS (PRODUCTION SAFE VERSION)
# =====================================================

def build_dim_users(users, sessions, orders):

    print("Building dim_users_enriched...")

    # Lifetime sessions
    session_counts = sessions.groupby("user_id")["session_id"].count().reset_index()
    session_counts.columns = ["user_id", "lifetime_sessions"]

    # Lifetime orders
    order_counts = orders.groupby("user_id")["order_id"].count().reset_index()
    order_counts.columns = ["user_id", "lifetime_orders"]

    # Order timestamps
    orders["order_ts"] = pd.to_datetime(orders["order_ts"])

    order_dates = orders.groupby("user_id").agg(
        first_order_date=("order_ts", "min"),
        last_order_date=("order_ts", "max")
    ).reset_index()

    # Total spend
    user_spend = orders.groupby("user_id")["net_amount"].sum().reset_index()
    user_spend.columns = ["user_id", "total_spend"]

    # Merge everything
    dim_users = users.merge(session_counts, on="user_id", how="left")
    dim_users = dim_users.merge(order_counts, on="user_id", how="left")
    dim_users = dim_users.merge(order_dates, on="user_id", how="left")
    dim_users = dim_users.merge(user_spend, on="user_id", how="left")

    dim_users[["lifetime_sessions", "lifetime_orders", "total_spend"]] = \
        dim_users[["lifetime_sessions", "lifetime_orders", "total_spend"]].fillna(0)

    # Repeat purchase flag
    dim_users["repeat_rate_flag"] = dim_users["lifetime_orders"] >= 2

    # Improved value band segmentation
    median_spend = dim_users["total_spend"].median()

    dim_users["user_value_band"] = np.where(
        dim_users["total_spend"] == 0,
        "No Purchase",
        np.where(
            dim_users["total_spend"] <= median_spend,
            "Low",
            "High"
        )
    )

    print("dim_users_enriched created.")
    return dim_users


# =====================================================
# MAIN EXECUTION
# =====================================================

def main():

    users, sessions, events, orders, order_items, campaigns, products = load_data()

    sessions = clean_sessions(sessions)

    fact_sessions = build_fact_sessions(sessions, events, orders)
    fact_orders = build_fact_orders(orders, order_items, products)
    dim_users = build_dim_users(users, sessions, orders)

    os.makedirs("data", exist_ok=True)

    fact_sessions.to_csv("data/fact_sessions.csv", index=False)
    fact_orders.to_csv("data/fact_orders.csv", index=False)
    dim_users.to_csv("data/dim_users_enriched.csv", index=False)

    print("All curated datasets saved.")
    print("ETL COMPLETE SUCCESSFULLY.")


if __name__ == "__main__":
    main()