-- QQQ 0DTE trading system schema for MySQL 8.4+
-- Application timestamps are written and interpreted as UTC.

CREATE DATABASE IF NOT EXISTS `qqq`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE `qqq`;

CREATE TABLE IF NOT EXISTS `system_events` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `created_at` DATETIME NOT NULL,
  `kind` VARCHAR(64) NOT NULL,
  `message` TEXT NOT NULL,
  `details` JSON NOT NULL,
  CONSTRAINT `pk_system_events` PRIMARY KEY (`id`),
  KEY `ix_system_events_created_at` (`created_at`),
  KEY `ix_system_events_kind` (`kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `trade_signals` (
  `intent_id` CHAR(32) NOT NULL,
  `decision_at` DATETIME NOT NULL,
  `action` VARCHAR(8) NOT NULL,
  `direction` VARCHAR(8) NOT NULL,
  `symbol` VARCHAR(64) NOT NULL,
  `reference_price` DECIMAL(18,6) NOT NULL,
  `quantity` INT NOT NULL,
  `status` VARCHAR(16) NOT NULL,
  `reason` VARCHAR(64) NOT NULL,
  `indicators` JSON NOT NULL,
  CONSTRAINT `pk_trade_signals` PRIMARY KEY (`intent_id`),
  KEY `ix_trade_signals_decision_at` (`decision_at`),
  KEY `ix_trade_signals_action` (`action`),
  KEY `ix_trade_signals_symbol` (`symbol`),
  KEY `ix_trade_signals_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `order_intents` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `intent_id` CHAR(32) NOT NULL,
  `created_at` DATETIME NOT NULL,
  `symbol` VARCHAR(64) NOT NULL,
  `side` VARCHAR(8) NOT NULL,
  `quantity` INT NOT NULL,
  `limit_price` DECIMAL(18,6) NOT NULL,
  `reason` VARCHAR(64) NOT NULL,
  CONSTRAINT `pk_order_intents` PRIMARY KEY (`id`),
  KEY `ix_order_intents_intent_id` (`intent_id`),
  KEY `ix_order_intents_created_at` (`created_at`),
  KEY `ix_order_intents_symbol` (`symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `broker_orders` (
  `order_id` VARCHAR(64) NOT NULL,
  `intent_id` CHAR(32) NOT NULL,
  `updated_at` DATETIME NOT NULL,
  `symbol` VARCHAR(64) NOT NULL,
  `side` VARCHAR(8) NOT NULL,
  `quantity` INT NOT NULL,
  `filled_quantity` INT NOT NULL,
  `average_price` DECIMAL(18,6) NULL,
  `status` VARCHAR(32) NOT NULL,
  `submitted_at` DATETIME NOT NULL,
  CONSTRAINT `pk_broker_orders` PRIMARY KEY (`order_id`),
  KEY `ix_broker_orders_intent_id` (`intent_id`),
  KEY `ix_broker_orders_updated_at` (`updated_at`),
  KEY `ix_broker_orders_symbol` (`symbol`),
  KEY `ix_broker_orders_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `executions` (
  `id` VARCHAR(96) NOT NULL,
  `order_id` VARCHAR(64) NOT NULL,
  `intent_id` CHAR(32) NOT NULL,
  `symbol` VARCHAR(64) NOT NULL,
  `side` VARCHAR(8) NOT NULL,
  `cumulative_quantity` INT NOT NULL,
  `price` DECIMAL(18,6) NOT NULL,
  `recorded_at` DATETIME NOT NULL,
  CONSTRAINT `pk_executions` PRIMARY KEY (`id`),
  KEY `ix_executions_order_id` (`order_id`),
  KEY `ix_executions_intent_id` (`intent_id`),
  KEY `ix_executions_symbol` (`symbol`),
  KEY `ix_executions_recorded_at` (`recorded_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `risk_snapshots` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `created_at` DATETIME NOT NULL,
  `equity` DECIMAL(20,4) NOT NULL,
  `cash_usd` DECIMAL(20,4) NOT NULL,
  `day_realized_pnl` DECIMAL(20,4) NOT NULL,
  `day_unrealized_pnl` DECIMAL(20,4) NOT NULL,
  `halted` BOOL NOT NULL,
  CONSTRAINT `pk_risk_snapshots` PRIMARY KEY (`id`),
  KEY `ix_risk_snapshots_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `trade_summaries` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(64) NOT NULL,
  `direction` VARCHAR(8) NOT NULL,
  `quantity` INT NOT NULL,
  `entry_price` DECIMAL(18,6) NOT NULL,
  `exit_price` DECIMAL(18,6) NOT NULL,
  `pnl` DECIMAL(20,4) NOT NULL,
  `fees` DECIMAL(20,4) NOT NULL,
  `entry_at` DATETIME NOT NULL,
  `exit_at` DATETIME NOT NULL,
  `exit_reason` VARCHAR(64) NOT NULL,
  `slippage` DECIMAL(18,6) NOT NULL,
  `mae` DECIMAL(18,6) NOT NULL,
  `mfe` DECIMAL(18,6) NOT NULL,
  CONSTRAINT `pk_trade_summaries` PRIMARY KEY (`id`),
  KEY `ix_trade_summaries_symbol` (`symbol`),
  KEY `ix_trade_summaries_entry_at` (`entry_at`),
  KEY `ix_trade_summaries_exit_at` (`exit_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `config_versions` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `created_at` DATETIME NOT NULL,
  `values` JSON NOT NULL,
  `active` BOOL NOT NULL,
  CONSTRAINT `pk_config_versions` PRIMARY KEY (`id`),
  KEY `ix_config_versions_created_at` (`created_at`),
  KEY `ix_config_versions_active` (`active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `backtest_runs` (
  `id` VARCHAR(36) NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  `status` VARCHAR(24) NOT NULL,
  `progress` INT NOT NULL,
  `request` JSON NOT NULL,
  `result` JSON NULL,
  `error` TEXT NULL,
  CONSTRAINT `pk_backtest_runs` PRIMARY KEY (`id`),
  KEY `ix_backtest_runs_created_at` (`created_at`),
  KEY `ix_backtest_runs_updated_at` (`updated_at`),
  KEY `ix_backtest_runs_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` VARCHAR(32) NOT NULL,
  CONSTRAINT `alembic_version_pkc` PRIMARY KEY (`version_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO `alembic_version` (`version_num`)
SELECT '0004_trade_signals'
WHERE NOT EXISTS (SELECT 1 FROM `alembic_version`);


