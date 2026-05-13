"""
Workspace Scaffolder for Context DNA Vibe Coder Launch

Creates starter projects for beginners with zero coding setup.
Generates appropriate directory structures, starter files, and
Context DNA configuration based on project type selection.

Created: January 29, 2026
Part of: Vibe Coder Launch Initiative
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class ScaffoldTemplate:
    """Definition of a project template for scaffolding."""

    project_type_id: str
    name: str
    icon: str
    description: str
    directory_structure: List[str]  # List of directories to create
    starter_files: Dict[str, str]   # path -> content
    dependencies: List[str]         # Package dependencies
    dev_dependencies: List[str] = field(default_factory=list)
    scripts: Dict[str, str] = field(default_factory=dict)  # npm/pip scripts

    def to_dict(self) -> Dict:
        return {
            "project_type_id": self.project_type_id,
            "name": self.name,
            "icon": self.icon,
            "description": self.description,
            "directory_count": len(self.directory_structure),
            "file_count": len(self.starter_files),
            "dependency_count": len(self.dependencies),
        }


@dataclass
class ScaffoldResult:
    """Result of scaffolding a project."""

    success: bool
    project_path: Path
    message: str
    files_created: List[str] = field(default_factory=list)
    directories_created: List[str] = field(default_factory=list)
    git_initialized: bool = False
    context_dna_initialized: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "project_path": str(self.project_path),
            "message": self.message,
            "files_created": self.files_created,
            "directories_created": self.directories_created,
            "git_initialized": self.git_initialized,
            "context_dna_initialized": self.context_dna_initialized,
            "error": self.error,
        }


class WorkspaceScaffolder:
    """
    Create starter projects for beginners.

    Provides project templates for common development scenarios,
    generates appropriate directory structures, and initializes
    Context DNA configuration.
    """

    def __init__(self):
        self.templates = self._build_templates()

    def _build_templates(self) -> Dict[str, ScaffoldTemplate]:
        """Build all available project templates."""
        return {
            "python_starter": self._template_python_starter(),
            "fastapi": self._template_fastapi(),
            "ml_python": self._template_ml_python(),
            "nextjs": self._template_nextjs(),
            "expo": self._template_expo(),
        }

    # =========================================================================
    # Template Definitions
    # =========================================================================

    def _template_python_starter(self) -> ScaffoldTemplate:
        """Minimal Python project template."""
        return ScaffoldTemplate(
            project_type_id="python_starter",
            name="Python Starter",
            icon="💡",
            description="Minimal Python project - great for learning and quick scripts",
            directory_structure=[
                "src",
                "tests",
                "docs",
            ],
            starter_files={
                "src/__init__.py": "",
                "src/main.py": '''"""
Main entry point for the application.

Run with: python src/main.py
"""


def main():
    """Main function - your code starts here!"""
    print("Hello, World! 🎉")
    print("Welcome to your new Python project!")
    print()
    print("Start coding in this file, or create new modules in src/")


if __name__ == "__main__":
    main()
''',
                "tests/__init__.py": "",
                "tests/test_main.py": '''"""
Tests for main module.

Run with: pytest
"""

from src.main import main


def test_main_runs():
    """Test that main function runs without error."""
    # This test just verifies main() doesn't crash
    main()  # Should print and return without error
''',
                "requirements.txt": '''# Project dependencies
# Add packages here, one per line
# Example: requests>=2.28.0

pytest>=7.0.0
''',
                ".gitignore": self._gitignore_python(),
                "README.md": '''# My Python Project

A new Python project created with Context DNA.

## Getting Started

1. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\\Scripts\\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the project:
   ```bash
   python src/main.py
   ```

## Running Tests

```bash
pytest
```

## Project Structure

```
my-project/
├── src/           ← Your code goes here
│   ├── __init__.py
│   └── main.py    ← Start here!
├── tests/         ← Your tests go here
├── docs/          ← Documentation
└── requirements.txt
```

Happy coding! 🚀
''',
                ".env.example": '''# Environment variables
# Copy to .env and fill in values
# Never commit .env to git!

# Example:
# API_KEY=your-api-key-here
# DEBUG=true
''',
            },
            dependencies=["pytest>=7.0.0"],
        )

    def _template_fastapi(self) -> ScaffoldTemplate:
        """FastAPI REST API template."""
        return ScaffoldTemplate(
            project_type_id="fastapi",
            name="FastAPI",
            icon="🔧",
            description="Modern Python API with FastAPI - fast, async, auto-documented",
            directory_structure=[
                "app",
                "app/routers",
                "app/models",
                "tests",
                "docs",
            ],
            starter_files={
                "app/__init__.py": "",
                "app/main.py": '''"""
FastAPI Application

Run with: uvicorn app.main:app --reload
API docs at: http://localhost:8000/docs
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="My API",
    description="A new API created with Context DNA",
    version="0.1.0",
)

# Allow CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Root endpoint - API health check."""
    return {"message": "Hello, World!", "status": "ok"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


# Add your routes below!
# Example:
# @app.get("/items/{item_id}")
# async def read_item(item_id: int):
#     return {"item_id": item_id}
''',
                "app/routers/__init__.py": "",
                "app/models/__init__.py": "",
                "tests/__init__.py": "",
                "tests/test_main.py": '''"""
Tests for main API.

Run with: pytest
"""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_root():
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health():
    """Test health endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
''',
                "requirements.txt": '''# FastAPI and dependencies
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
python-multipart>=0.0.6

# Testing
pytest>=7.0.0
httpx>=0.24.0

# Optional: Add more as needed
# sqlalchemy>=2.0.0
# pydantic-settings>=2.0.0
''',
                ".gitignore": self._gitignore_python(),
                "README.md": '''# My FastAPI Project

A REST API built with FastAPI and Context DNA.

## Getting Started

1. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\\Scripts\\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the development server:
   ```bash
   uvicorn app.main:app --reload
   ```

4. Open the API docs:
   - Swagger UI: http://localhost:8000/docs
   - ReDoc: http://localhost:8000/redoc

## Running Tests

```bash
pytest
```

## Project Structure

```
my-api/
├── app/
│   ├── __init__.py
│   ├── main.py        ← Main FastAPI app
│   ├── routers/       ← Route handlers
│   └── models/        ← Pydantic models
├── tests/
└── requirements.txt
```

Happy building! 🚀
''',
                ".env.example": '''# Environment variables
# Copy to .env and fill in values

# API Settings
# DEBUG=true
# DATABASE_URL=://:pass@/db
''',
            },
            dependencies=[
                "fastapi>=0.100.0",
                "uvicorn[standard]>=0.23.0",
                "pytest>=7.0.0",
                "httpx>=0.24.0",
            ],
        )

    def _template_ml_python(self) -> ScaffoldTemplate:
        """Machine Learning / AI Python template."""
        return ScaffoldTemplate(
            project_type_id="ml_python",
            name="AI/ML Python",
            icon="🤖",
            description="Python project for AI/ML with Jupyter notebooks",
            directory_structure=[
                "notebooks",
                "src",
                "data",
                "models",
                "tests",
                "docs",
            ],
            starter_files={
                "src/__init__.py": "",
                "src/main.py": '''"""
Main entry point for ML project.

This file can be used for running training scripts
or serving predictions.
"""

import os
from pathlib import Path


# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"
NOTEBOOKS_DIR = PROJECT_ROOT / "notebooks"


def main():
    """Main function."""
    print("🤖 AI/ML Project Ready!")
    print()
    print("Project structure:")
    print(f"  Data:      {DATA_DIR}")
    print(f"  Models:    {MODELS_DIR}")
    print(f"  Notebooks: {NOTEBOOKS_DIR}")
    print()
    print("Next steps:")
    print("  1. Add your data to the data/ folder")
    print("  2. Open notebooks/exploration.ipynb to start exploring")
    print("  3. Build your model in src/")


if __name__ == "__main__":
    main()
''',
                "notebooks/exploration.ipynb": json.dumps({
                    "cells": [
                        {
                            "cell_type": "markdown",
                            "metadata": {},
                            "source": [
                                "# Data Exploration Notebook\n",
                                "\n",
                                "Welcome to your AI/ML project! 🤖\n",
                                "\n",
                                "This notebook is for exploring your data and experimenting with models."
                            ]
                        },
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "metadata": {},
                            "outputs": [],
                            "source": [
                                "# Import common libraries\n",
                                "import numpy as np\n",
                                "import pandas as pd\n",
                                "import matplotlib.pyplot as plt\n",
                                "\n",
                                "# Set display options\n",
                                "pd.set_option('display.max_columns', 50)\n",
                                "%matplotlib inline"
                            ]
                        },
                        {
                            "cell_type": "markdown",
                            "metadata": {},
                            "source": [
                                "## Load Your Data\n",
                                "\n",
                                "Put your data files in the `data/` folder and load them here."
                            ]
                        },
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "metadata": {},
                            "outputs": [],
                            "source": [
                                "# Example: Load a CSV file\n",
                                "# df = pd.read_csv('../data/your_data.csv')\n",
                                "# df.head()"
                            ]
                        },
                        {
                            "cell_type": "markdown",
                            "metadata": {},
                            "source": [
                                "## Explore & Analyze\n",
                                "\n",
                                "Add your exploration code below!"
                            ]
                        },
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "metadata": {},
                            "outputs": [],
                            "source": [
                                "# Your code here!\n",
                                "print('Ready to explore! 🚀')"
                            ]
                        }
                    ],
                    "metadata": {
                        "kernelspec": {
                            "display_name": "Python 3",
                            "language": "python",
                            "name": "python3"
                        },
                        "language_info": {
                            "name": "python",
                            "version": "3.11.0"
                        }
                    },
                    "nbformat": 4,
                    "nbformat_minor": 4
                }, indent=2),
                "data/.gitkeep": "# This file keeps the data folder in git\n# Add your datasets here\n",
                "models/.gitkeep": "# This file keeps the models folder in git\n# Saved models go here\n",
                "tests/__init__.py": "",
                "requirements.txt": '''# Core ML libraries
numpy>=1.24.0
pandas>=2.0.0
scikit-learn>=1.3.0
matplotlib>=3.7.0
seaborn>=0.12.0

# Jupyter
jupyter>=1.0.0
ipykernel>=6.25.0

# Testing
pytest>=7.0.0

# Optional: Deep Learning (uncomment as needed)
# torch>=2.0.0
# tensorflow>=2.13.0
# transformers>=4.30.0

# Optional: Data processing
# opencv-python>=4.8.0
# pillow>=10.0.0
''',
                ".gitignore": self._gitignore_python() + '''
# ML-specific
*.h5
*.hdf5
*.pkl
*.joblib
*.onnx
*.pt
*.pth
checkpoints/
wandb/

# Data files (may be large)
*.csv
*.parquet
*.arrow
!data/.gitkeep

# Jupyter
.ipynb_checkpoints/
''',
                "README.md": '''# My AI/ML Project

An AI/ML project created with Context DNA.

## Getting Started

1. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\\Scripts\\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Start Jupyter:
   ```bash
   jupyter notebook
   ```

4. Open `notebooks/exploration.ipynb` to start exploring!

## Project Structure

```
my-ml-project/
├── notebooks/     ← Jupyter notebooks for exploration
├── src/           ← Python modules
├── data/          ← Datasets (not committed to git)
├── models/        ← Saved model files
├── tests/         ← Unit tests
└── docs/          ← Documentation
```

## Adding Deep Learning

Uncomment the relevant libraries in `requirements.txt`:
- PyTorch: `torch>=2.0.0`
- TensorFlow: `tensorflow>=2.13.0`
- Hugging Face: `transformers>=4.30.0`

Happy modeling! 🤖
''',
                ".env.example": '''# Environment variables

# Optional: API keys for cloud services
# Context_DNA_OPENAI=sk-...
# WANDB_API_KEY=...
''',
            },
            dependencies=[
                "numpy>=1.24.0",
                "pandas>=2.0.0",
                "scikit-learn>=1.3.0",
                "matplotlib>=3.7.0",
                "jupyter>=1.0.0",
                "pytest>=7.0.0",
            ],
        )

    def _template_nextjs(self) -> ScaffoldTemplate:
        """Next.js web application template."""
        return ScaffoldTemplate(
            project_type_id="nextjs",
            name="Web App (Next.js)",
            icon="🌐",
            description="Modern React web app with Next.js and TypeScript",
            directory_structure=[
                "app",
                "components",
                "lib",
                "public",
                "styles",
            ],
            starter_files={
                "app/layout.tsx": '''import type { Metadata } from 'next'
import '../styles/globals.css'

export const metadata: Metadata = {
  title: 'My App',
  description: 'Created with Context DNA',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
''',
                "app/page.tsx": '''export default function Home() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8">
      <h1 className="text-4xl font-bold mb-4">
        Welcome to Your App! 🎉
      </h1>
      <p className="text-lg text-gray-600 mb-8">
        Start building by editing <code className="bg-gray-100 px-2 py-1 rounded">app/page.tsx</code>
      </p>
      <div className="flex gap-4">
        <a
          href="https://nextjs.org/docs"
          className="px-6 py-3 bg-black text-white rounded-lg hover:bg-gray-800"
          target="_blank"
          rel="noopener noreferrer"
        >
          Next.js Docs
        </a>
        <a
          href="https://react.dev"
          className="px-6 py-3 border border-gray-300 rounded-lg hover:bg-gray-50"
          target="_blank"
          rel="noopener noreferrer"
        >
          React Docs
        </a>
      </div>
    </main>
  )
}
''',
                "components/.gitkeep": "# React components go here\n",
                "lib/.gitkeep": "# Utility functions and shared code go here\n",
                "public/.gitkeep": "# Static assets (images, fonts) go here\n",
                "styles/globals.css": '''@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --foreground-rgb: 0, 0, 0;
  --background-rgb: 255, 255, 255;
}

body {
  color: rgb(var(--foreground-rgb));
  background: rgb(var(--background-rgb));
}
''',
                "package.json": json.dumps({
                    "name": "my-nextjs-app",
                    "version": "0.1.0",
                    "private": True,
                    "scripts": {
                        "dev": "next dev",
                        "build": "next build",
                        "start": "next start",
                        "lint": "next lint"
                    },
                    "dependencies": {
                        "next": "14.0.0",
                        "react": "^18",
                        "react-dom": "^18"
                    },
                    "devDependencies": {
                        "@types/node": "^20",
                        "@types/react": "^18",
                        "@types/react-dom": "^18",
                        "autoprefixer": "^10.0.1",
                        "postcss": "^8",
                        "tailwindcss": "^3.3.0",
                        "typescript": "^5"
                    }
                }, indent=2),
                "tsconfig.json": json.dumps({
                    "compilerOptions": {
                        "lib": ["dom", "dom.iterable", "esnext"],
                        "allowJs": True,
                        "skipLibCheck": True,
                        "strict": True,
                        "noEmit": True,
                        "esModuleInterop": True,
                        "module": "esnext",
                        "moduleResolution": "bundler",
                        "resolveJsonModule": True,
                        "isolatedModules": True,
                        "jsx": "preserve",
                        "incremental": True,
                        "plugins": [{"name": "next"}],
                        "paths": {"@/*": ["./*"]}
                    },
                    "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
                    "exclude": ["node_modules"]
                }, indent=2),
                "tailwind.config.js": '''/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
''',
                "postcss.config.js": '''module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
''',
                "next.config.js": '''/** @type {import('next').NextConfig} */
const nextConfig = {}

module.exports = nextConfig
''',
                ".gitignore": '''# Dependencies
node_modules/
.pnp
.pnp.js

# Build
.next/
out/
build/

# Misc
.DS_Store
*.pem

# Debug
npm-debug.log*
yarn-debug.log*
yarn-error.log*

# Local env
.env*.local
.env

# Vercel
.vercel

# TypeScript
*.tsbuildinfo
next-env.d.ts
''',
                "README.md": '''# My Next.js App

A modern web app built with Next.js and Context DNA.

## Getting Started

1. Install dependencies:
   ```bash
   npm install
   ```

2. Run the development server:
   ```bash
   npm run dev
   ```

3. Open [http://localhost:3000](http://localhost:3000) in your browser!

## Project Structure

```
my-app/
├── app/           ← Pages and routes
│   ├── layout.tsx ← Root layout
│   └── page.tsx   ← Home page (start here!)
├── components/    ← React components
├── lib/           ← Utility functions
├── public/        ← Static assets
└── styles/        ← CSS files
```

## Learn More

- [Next.js Documentation](https://nextjs.org/docs)
- [React Documentation](https://react.dev)
- [Tailwind CSS](https://tailwindcss.com/docs)

## Deploy

Deploy to [Vercel](https://vercel.com) with one click!

Happy coding! 🚀
''',
                ".env.example": '''# Environment variables
# Copy to .env.local and fill in values

# Example API keys
# NEXT_PUBLIC_API_URL=http://localhost:3000
''',
            },
            dependencies=[
                "next",
                "react",
                "react-dom",
            ],
            dev_dependencies=[
                "@types/node",
                "@types/react",
                "typescript",
                "tailwindcss",
            ],
        )

    def _template_expo(self) -> ScaffoldTemplate:
        """React Native / Expo mobile app template."""
        return ScaffoldTemplate(
            project_type_id="expo",
            name="Mobile App (Expo)",
            icon="📱",
            description="Cross-platform mobile app with React Native and Expo",
            directory_structure=[
                "app",
                "components",
                "hooks",
                "constants",
                "assets",
            ],
            starter_files={
                "app/_layout.tsx": '''import { Stack } from 'expo-router';

export default function RootLayout() {
  return (
    <Stack>
      <Stack.Screen
        name="index"
        options={{
          title: 'My App',
          headerShown: true
        }}
      />
    </Stack>
  );
}
''',
                "app/index.tsx": '''import { StyleSheet, Text, View, Pressable } from 'react-native';
import { useState } from 'react';

export default function HomeScreen() {
  const [count, setCount] = useState(0);

  return (
    <View style={styles.container}>
      <Text style={styles.title}>Welcome to Your App! 🎉</Text>
      <Text style={styles.subtitle}>
        Start building by editing app/index.tsx
      </Text>

      <View style={styles.counterContainer}>
        <Text style={styles.counterText}>Count: {count}</Text>
        <Pressable
          style={styles.button}
          onPress={() => setCount(c => c + 1)}
        >
          <Text style={styles.buttonText}>Tap me!</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 20,
    backgroundColor: '#fff',
  },
  title: {
    fontSize: 28,
    fontWeight: 'bold',
    marginBottom: 10,
  },
  subtitle: {
    fontSize: 16,
    color: '#666',
    textAlign: 'center',
    marginBottom: 40,
  },
  counterContainer: {
    alignItems: 'center',
  },
  counterText: {
    fontSize: 24,
    marginBottom: 20,
  },
  button: {
    backgroundColor: '#000',
    paddingHorizontal: 30,
    paddingVertical: 15,
    borderRadius: 10,
  },
  buttonText: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '600',
  },
});
''',
                "components/.gitkeep": "# React Native components go here\n",
                "hooks/.gitkeep": "# Custom React hooks go here\n",
                "constants/Colors.ts": '''export const Colors = {
  light: {
    text: '#000',
    background: '#fff',
    tint: '#2f95dc',
  },
  dark: {
    text: '#fff',
    background: '#000',
    tint: '#fff',
  },
};
''',
                "assets/.gitkeep": "# Images and other assets go here\n",
                "package.json": json.dumps({
                    "name": "my-expo-app",
                    "version": "1.0.0",
                    "main": "expo-router/entry",
                    "scripts": {
                        "start": "expo start",
                        "android": "expo start --android",
                        "ios": "expo start --ios",
                        "web": "expo start --web"
                    },
                    "dependencies": {
                        "expo": "~50.0.0",
                        "expo-router": "~3.4.0",
                        "expo-status-bar": "~1.11.0",
                        "react": "18.2.0",
                        "react-native": "0.73.0",
                        "react-native-safe-area-context": "4.8.2",
                        "react-native-screens": "~3.29.0"
                    },
                    "devDependencies": {
                        "@babel/core": "^7.20.0",
                        "@types/react": "~18.2.0",
                        "typescript": "^5.1.0"
                    },
                    "private": True
                }, indent=2),
                "tsconfig.json": json.dumps({
                    "extends": "expo/tsconfig.base",
                    "compilerOptions": {
                        "strict": True
                    }
                }, indent=2),
                "app.json": json.dumps({
                    "expo": {
                        "name": "my-expo-app",
                        "slug": "my-expo-app",
                        "version": "1.0.0",
                        "orientation": "portrait",
                        "scheme": "myapp",
                        "userInterfaceStyle": "automatic",
                        "splash": {
                            "resizeMode": "contain",
                            "backgroundColor": "#ffffff"
                        },
                        "ios": {
                            "supportsTablet": True
                        },
                        "android": {
                            "adaptiveIcon": {
                                "backgroundColor": "#ffffff"
                            }
                        },
                        "web": {
                            "bundler": "metro"
                        }
                    }
                }, indent=2),
                ".gitignore": '''# Dependencies
node_modules/

# Expo
.expo/
dist/
web-build/

# Native builds
*.orig.*
*.jks
*.p8
*.p12
*.key
*.mobileprovision

# Metro
.metro-health-check*

# Debug
npm-debug.*
yarn-debug.*
yarn-error.*

# Misc
.DS_Store
*.pem

# Local env
.env*.local
.env
''',
                "README.md": '''# My Expo App

A cross-platform mobile app built with Expo and Context DNA.

## Getting Started

1. Install dependencies:
   ```bash
   npm install
   ```

2. Start the development server:
   ```bash
   npm start
   ```

3. Run on your device:
   - **iOS**: Press `i` to open in iOS Simulator
   - **Android**: Press `a` to open in Android Emulator
   - **Phone**: Scan the QR code with Expo Go app

## Project Structure

```
my-app/
├── app/           ← Screens and navigation
│   ├── _layout.tsx
│   └── index.tsx  ← Home screen (start here!)
├── components/    ← Reusable components
├── hooks/         ← Custom React hooks
├── constants/     ← App constants
└── assets/        ← Images and fonts
```

## Learn More

- [Expo Documentation](https://docs.expo.dev)
- [React Native](https://reactnative.dev)
- [Expo Router](https://expo.github.io/router/docs)

Happy building! 📱
''',
                ".env.example": '''# Environment variables
# Copy to .env and fill in values

# API Configuration
# API_URL=https://api.example.com
''',
            },
            dependencies=[
                "expo",
                "expo-router",
                "react",
                "react-native",
            ],
            dev_dependencies=[
                "@babel/core",
                "@types/react",
                "typescript",
            ],
        )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def _gitignore_python(self) -> str:
        """Standard Python .gitignore."""
        return '''# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# Virtual environments
venv/
.venv/
env/
.env/

# Distribution / packaging
build/
dist/
*.egg-info/
*.egg

# PyInstaller
*.manifest
*.spec

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage reports
htmlcov/
.tox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
.hypothesis/
.pytest_cache/

# Environments
.env
.venv
env/
venv/

# IDE
.idea/
.vscode/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Project-specific
*.log
*.sqlite3
'''

    # =========================================================================
    # Public Methods
    # =========================================================================

    def get_available_templates(self) -> List[ScaffoldTemplate]:
        """Get all available project templates."""
        return list(self.templates.values())

    def get_template(self, template_id: str) -> Optional[ScaffoldTemplate]:
        """Get a specific template by ID."""
        return self.templates.get(template_id)

    def get_template_choices(self) -> List[Dict]:
        """Get templates formatted for user selection."""
        return [
            {
                "id": t.project_type_id,
                "label": f"{t.icon} {t.name}",
                "description": t.description,
            }
            for t in self.templates.values()
        ]

    def scaffold_project(
        self,
        template_id: str,
        project_name: str,
        location: Path,
        init_git: bool = True,
        init_context_dna: bool = True,
    ) -> ScaffoldResult:
        """
        Create a new project from a template.

        Args:
            template_id: ID of the template to use
            project_name: Name for the new project
            location: Parent directory where project will be created
            init_git: Whether to initialize git repository
            init_context_dna: Whether to initialize Context DNA config

        Returns:
            ScaffoldResult with details of what was created
        """
        template = self.get_template(template_id)
        if not template:
            return ScaffoldResult(
                success=False,
                project_path=location,
                message=f"Unknown template: {template_id}",
                error=f"Template '{template_id}' not found. Available: {list(self.templates.keys())}"
            )

        # Create project directory
        project_path = location / self._sanitize_name(project_name)

        if project_path.exists():
            return ScaffoldResult(
                success=False,
                project_path=project_path,
                message=f"Directory already exists: {project_path}",
                error="Project directory already exists. Choose a different name or location."
            )

        try:
            files_created = []
            directories_created = []

            # Create project root
            project_path.mkdir(parents=True, exist_ok=True)
            directories_created.append(str(project_path))

            # Create directory structure
            for directory in template.directory_structure:
                dir_path = project_path / directory
                dir_path.mkdir(parents=True, exist_ok=True)
                directories_created.append(str(dir_path))

            # Create starter files
            for file_path, content in template.starter_files.items():
                full_path = project_path / file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)

                # Update project name in certain files
                if file_path in ["package.json", "app.json"]:
                    content = content.replace("my-nextjs-app", self._sanitize_name(project_name))
                    content = content.replace("my-expo-app", self._sanitize_name(project_name))

                full_path.write_text(content, encoding='utf-8')
                files_created.append(str(full_path))

            # Initialize git
            git_initialized = False
            if init_git:
                git_initialized = self._init_git(project_path)

            # Initialize Context DNA
            context_dna_initialized = False
            if init_context_dna:
                context_dna_initialized = self._init_context_dna(project_path, template)
                if context_dna_initialized:
                    files_created.append(str(project_path / ".context-dna" / "settings.json"))
                    files_created.append(str(project_path / ".context-dna" / "hierarchy_profile.json"))
                    directories_created.append(str(project_path / ".context-dna"))

            return ScaffoldResult(
                success=True,
                project_path=project_path,
                message=f"Created {template.name} project: {project_name}",
                files_created=files_created,
                directories_created=directories_created,
                git_initialized=git_initialized,
                context_dna_initialized=context_dna_initialized,
            )

        except Exception as e:
            return ScaffoldResult(
                success=False,
                project_path=project_path,
                message="Failed to create project",
                error=str(e)[:200],
            )

    def _sanitize_name(self, name: str) -> str:
        """Sanitize project name for file system and package managers."""
        # Replace spaces and special chars
        sanitized = name.lower().replace(" ", "-")
        # Remove anything that's not alphanumeric or hyphen
        sanitized = "".join(c for c in sanitized if c.isalnum() or c == "-")
        # Remove consecutive hyphens
        while "--" in sanitized:
            sanitized = sanitized.replace("--", "-")
        # Remove leading/trailing hyphens
        sanitized = sanitized.strip("-")
        return sanitized or "my-project"

    def _init_git(self, project_path: Path) -> bool:
        """Initialize git repository in project."""
        try:
            subprocess.run(
                ["git", "init"],
                cwd=project_path,
                capture_output=True,
                check=True,
            )
            return True
        except Exception:
            return False

    def _init_context_dna(self, project_path: Path, template: ScaffoldTemplate) -> bool:
        """Initialize Context DNA configuration in project."""
        try:
            config_dir = project_path / ".context-dna"
            config_dir.mkdir(exist_ok=True)

            # Create settings.json
            settings = {
                "version": "1.0",
                "created": datetime.now().isoformat(),
                "project_type": template.project_type_id,
                "project_name": project_path.name,
                "injection_enabled": True,
                "volume_level": "silver_platter",
            }
            (config_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding='utf-8')

            # Create hierarchy_profile.json
            hierarchy = {
                "version": "1.0",
                "project_type": template.project_type_id,
                "detected_patterns": {
                    "framework": template.name,
                    "language": "python" if "python" in template.project_type_id else "typescript",
                },
                "directory_purposes": {
                    "src": "source_code",
                    "tests": "tests",
                    "docs": "documentation",
                },
            }
            (config_dir / "hierarchy_profile.json").write_text(json.dumps(hierarchy, indent=2), encoding='utf-8')

            return True
        except Exception:
            return False


# =============================================================================
# Module-Level Convenience Functions
# =============================================================================

_scaffolder = None


def get_scaffolder() -> WorkspaceScaffolder:
    """Get or create the global WorkspaceScaffolder instance."""
    global _scaffolder
    if _scaffolder is None:
        _scaffolder = WorkspaceScaffolder()
    return _scaffolder


def scaffold_project(
    template_id: str,
    project_name: str,
    location: Path,
) -> ScaffoldResult:
    """Scaffold a new project."""
    return get_scaffolder().scaffold_project(template_id, project_name, location)


def get_templates() -> List[Dict]:
    """Get available template choices."""
    return get_scaffolder().get_template_choices()


# =============================================================================
# CLI Interface
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Workspace Scaffolder - Create starter projects"
    )
    parser.add_argument("--list", action="store_true", help="List available templates")
    parser.add_argument("--scaffold", type=str, help="Template ID to scaffold")
    parser.add_argument("--name", type=str, default="my-project", help="Project name")
    parser.add_argument("--location", type=Path, default=Path.home() / "Projects", help="Parent directory")
    parser.add_argument("--no-git", action="store_true", help="Skip git initialization")
    parser.add_argument("--no-context-dna", action="store_true", help="Skip Context DNA initialization")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    scaffolder = WorkspaceScaffolder()

    if args.list:
        templates = scaffolder.get_template_choices()
        if args.json:
            print(json.dumps(templates, indent=2))
        else:
            print("Available Templates:")
            print("=" * 50)
            for t in templates:
                print(f"  {t['label']}")
                print(f"    ID: {t['id']}")
                print(f"    {t['description']}")
                print()

    elif args.scaffold:
        result = scaffolder.scaffold_project(
            template_id=args.scaffold,
            project_name=args.name,
            location=args.location,
            init_git=not args.no_git,
            init_context_dna=not args.no_context_dna,
        )

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            if result.success:
                print(f"✅ {result.message}")
                print(f"   Location: {result.project_path}")
                print(f"   Files created: {len(result.files_created)}")
                print(f"   Directories created: {len(result.directories_created)}")
                if result.git_initialized:
                    print("   ✓ Git initialized")
                if result.context_dna_initialized:
                    print("   ✓ Context DNA configured")
            else:
                print(f"❌ {result.message}")
                if result.error:
                    print(f"   Error: {result.error}")

    else:
        # Default: show templates in friendly format
        print("🚀 Workspace Scaffolder - Create Your First Project!")
        print("=" * 55)
        print()
        print("Available project types:")
        print()
        for t in scaffolder.get_template_choices():
            print(f"  {t['label']}")
            print(f"    → {t['description']}")
            print()
        print("Usage:")
        print("  python workspace_scaffolder.py --scaffold python_starter --name my-app")
        print("  python workspace_scaffolder.py --scaffold fastapi --name my-api")
        print("  python workspace_scaffolder.py --scaffold nextjs --name my-webapp")
