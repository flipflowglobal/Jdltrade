//! Solidity compilation engine.
//! Wraps `solc` to compile SovereignArb.sol, saves the ABI,
//! and provides an async deploy helper.

use std::fs;
use std::process::Command;

use eyre::Result;
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
pub struct CompiledContract {
    pub bytecode: String,          // hex-encoded, no 0x prefix
    pub abi:      serde_json::Value,
    pub name:     String,
}

pub struct SolcEngine {
    pub solc_path: String,
    pub out_dir:   String,
}

impl SolcEngine {
    pub fn new() -> Self {
        Self {
            solc_path: Self::detect_solc(),
            out_dir:   "abi".to_string(),
        }
    }

    fn detect_solc() -> String {
        let home = std::env::var("HOME").unwrap_or_default();
        let candidates = [
            "solc".to_string(),
            format!("{home}/.solc-select/artifacts/solc-0.8.24/solc-0.8.24"),
            format!("{home}/.local/bin/solc"),
            "/usr/local/bin/solc".to_string(),
        ];
        for path in &candidates {
            if Command::new(path).arg("--version").output().is_ok() {
                println!("[COMPILER] Using solc: {path}");
                return path.clone();
            }
        }
        println!("[COMPILER] Warning: solc not found, defaulting to 'solc'");
        "solc".to_string()
    }

    /// Compile `contract_path` with maximum optimisation flags.
    pub fn compile(&self, contract_path: &str) -> Result<CompiledContract> {
        fs::create_dir_all(&self.out_dir)?;

        println!("[COMPILER] Compiling {contract_path} ...");

        let output = Command::new(&self.solc_path)
            .args([
                "--combined-json",
                "abi,bin",
                "--optimize",
                "--optimize-runs",
                "1000000",
                "--evm-version",
                "cancun",   // EIP-1153 transient storage, EIP-4844 blob gas
                "--via-ir",  // Yul IR pipeline — highest optimisation
                "--base-path",
                ".",
                "--include-path",
                "node_modules",
                contract_path,
            ])
            .output()
            .map_err(|e| eyre::eyre!("Failed to run solc: {e}"))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(eyre::eyre!("Compilation failed:\n{stderr}"));
        }

        let json: serde_json::Value = serde_json::from_slice(&output.stdout)
            .map_err(|e| eyre::eyre!("Failed to parse solc JSON: {e}"))?;

        let contracts = json["contracts"]
            .as_object()
            .ok_or_else(|| eyre::eyre!("No 'contracts' key in solc output"))?;

        for (key, val) in contracts {
            if !key.contains("SovereignArb") {
                continue;
            }

            let bytecode = val["bin"]
                .as_str()
                .unwrap_or("")
                .to_string();

            // solc combined-json encodes the ABI as a JSON *string*
            let abi_raw = val["abi"].as_str().unwrap_or("[]");
            let abi: serde_json::Value = serde_json::from_str(abi_raw)
                .unwrap_or(serde_json::Value::Array(vec![]));

            fs::write(
                format!("{}/SovereignArb.json", self.out_dir),
                serde_json::to_string_pretty(&abi)?,
            )?;

            let size_bytes = bytecode.len() / 2;
            println!(
                "[COMPILER] ✓ SovereignArb compiled ({size_bytes} bytes, {} ABI entries)",
                abi.as_array().map(|a| a.len()).unwrap_or(0)
            );

            // Warn if approaching EIP-170 limit (24_576 bytes)
            if size_bytes > 20_000 {
                println!(
                    "[COMPILER] ⚠  Bytecode {size_bytes} bytes — approaching 24 576 limit"
                );
            }

            return Ok(CompiledContract {
                bytecode,
                abi,
                name: "SovereignArb".to_string(),
            });
        }

        Err(eyre::eyre!("SovereignArb not found in compiler output"))
    }

    /// Deploy the compiled contract and return its address.
    pub async fn deploy<M>(
        &self,
        compiled:      &CompiledContract,
        client:        std::sync::Arc<M>,
        profit_wallet: ethers::types::Address,
    ) -> Result<ethers::types::Address>
    where
        M: ethers::middleware::Middleware + 'static,
    {
        use ethers::prelude::*;

        let hex_clean = compiled.bytecode.trim_start_matches("0x");
        let mut deploy_bytes = hex::decode(hex_clean)
            .map_err(|e| eyre::eyre!("Hex decode failed: {e}"))?;

        // ABI-encode constructor argument: address _profitWallet
        let ctor_args = ethers::abi::encode(&[ethers::abi::Token::Address(profit_wallet)]);
        deploy_bytes.extend_from_slice(&ctor_args);

        let tx = TransactionRequest::new()
            .data(deploy_bytes)
            .gas(3_000_000u64);

        println!("[DEPLOY] Sending deployment transaction...");

        let receipt = client
            .send_transaction(tx, None)
            .await
            .map_err(|e| eyre::eyre!("send_transaction failed: {e}"))?
            .await
            .map_err(|e| eyre::eyre!("Waiting for receipt failed: {e}"))?
            .ok_or_else(|| eyre::eyre!("Transaction confirmed but receipt was None"))?;

        let addr = receipt
            .contract_address
            .ok_or_else(|| eyre::eyre!("Receipt had no contract_address"))?;

        let gas_used = receipt.gas_used.unwrap_or_default();
        println!("[DEPLOY] ✓ SovereignArb @ {addr:?}  (gas used: {gas_used})");
        Ok(addr)
    }
}

impl Default for SolcEngine {
    fn default() -> Self { Self::new() }
}
