from pathlib import Path
import aiofiles


async def load_prompt(name: str) -> str:
    file_path = Path(__file__).parent / f"{name}.md"
    async with aiofiles.open(file_path, "r", encoding="utf-8") as fp:
        return await fp.read()
    

if __name__ == "__main__":
    import asyncio
    async def test():
        prompt = await load_prompt("correct_sql")
        print(prompt)
    asyncio.run(test())