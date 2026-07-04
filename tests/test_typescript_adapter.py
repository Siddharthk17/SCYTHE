from ctx_engine.languages.registry import get_parser
from ctx_engine.languages.typescript_adapter import TypeScriptAdapter

def test_typescript_adapter_extraction():
    src = """
import { Request, Response } from "express";

export interface User {
    id: number;
    name: string;
}

export class UserService {
    private db: any;
    
    constructor(db: any) {
        this.db = db;
    }
    
    async getUser(id: number): Promise<User> {
        return this.db.find(id);
    }
}

export const logger = (msg: string): void => {
    console.log(msg);
};
"""
    parser = get_parser("typescript")
    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)

    adapter = TypeScriptAdapter()
    struct = adapter.extract(tree, source_bytes)

    # 1. Assert exports
    assert set(struct.exports) == {"UserService", "logger"}

    # 2. Assert functions and mutations
    funcs = {f"{f.class_name}::{f.name}" if f.class_name else f.name: f for f in struct.functions}

    assert "UserService::constructor" in funcs
    constructor_func = funcs["UserService::constructor"]
    assert constructor_func.signature == "constructor(db: any)"
    assert constructor_func.mutates == ["this.db"]

    assert "UserService::getUser" in funcs
    get_user_func = funcs["UserService::getUser"]
    assert get_user_func.signature == "async getUser(id: number): Promise<User>"

    assert "logger" in funcs
    logger_func = funcs["logger"]
    assert logger_func.signature == "(msg: string): void =>"
