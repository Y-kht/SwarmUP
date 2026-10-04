## Contribution Guidelines

1. **Fork the Repository**: Start by forking the repository to your own GitHub account. This allows you to freely experiment with changes without affecting the original project.

2. **Clone the Repository**: Clone your forked repository to your local machine using the following command:
   ```
   git clone <your-forked-repo-url>
    ```

3. **Create a New Branch**: Before making any changes, create a new branch for your work. Use the following command to create a new branch:
   ```
   git checkout -b <your-branch-name>
   ```
   - **Branch Naming Convention**: Use descriptive names for your branches that reflect the purpose of the changes you are making. For example, if you are fixing a bug related to user authentication, you could name your branch `fix/user-authentication-bug`. If you are adding a new feature for data visualization, you could name your branch `feature/data-visualization`. Avoid using generic names like `new-feature` or `bug-fix`, as they do not provide enough context about the changes being made. Use lowercase letters and hyphens to separate words in branch names.

4. **Make Changes**: Implement your changes or additions to the codebase. Ensure that your code follows the stipulated coding style and conventions used in the project (explained below).

5. **Commit Your Changes**: After making your changes, commit them with a clear and concise commit message that describes the purpose of the changes. Use the following command to commit your changes:
   ```
   git add .
   git commit -m "Your commit message"
   ```
   - **Commit Message Guidelines**: Write commit messages that are clear, concise, and descriptive. Use the imperative mood (e.g., "Fix bug" instead of "Fixed bug" or "Fixes bug"). Include relevant information about the changes made, such as the issue number or a brief description of the problem being addressed. Avoid vague messages like "Update code" or "Fix stuff". A good commit message should provide enough context for others to understand the purpose of the changes without having to read the code itself.

6. **Push Changes to Your Fork**: Push your changes to your forked repository on GitHub using the following command:
   ```
   git push origin <your-branch-name>
   ```

#### **Never push directly to the main branch of the original repository. Always work on a separate branch and submit a pull request for review. Any push to the main branch will be rejected without review.**

7. **Submit a Pull Request**: Once your changes are pushed to your forked repository, navigate to the original repository on GitHub and submit a pull request. Provide a detailed description of the changes you made and why they are beneficial to the project.

8. **Code Review**: The maintainers of the project will review your pull request. They may provide feedback or request changes before merging your contributions into the main codebase. Be open to constructive criticism and be prepared to make necessary adjustments. The shared objective is to improve the project and ensure that it meets the quality standards set by the maintainers so as to benefit the community. No comment is personal!

9. **Merge and Celebrate**: Once your pull request is approved and merged, celebrate your contribution to the project! Your efforts help improve the project and benefit the community.

### Coding Style and Conventions
- **Follow Existing Style**: Adhere to the coding style and conventions already established in the project. This includes naming conventions, indentation, and formatting. Consistency in code style makes it easier for others to read and understand your code.
  - **Use Meaningful Names**: Choose descriptive and meaningful names for variables, functions, and classes. This enhances code readability and helps others understand the purpose of your code. But avoid overly long names that can make the code cumbersome to read.
  - **Comment Your Code**: Include comments where necessary to explain complex logic or important decisions in your code. This helps others (and your future self) understand the reasoning behind your implementation. However, avoid verbose docstrings and unnecessary comments for simple code that is self-explanatory. Comments are MOSTLY not needed and they make the code cluttered. Use them only when necessary.
  - **Write Tests**: If applicable, write tests for your code to ensure its correctness and reliability. This helps catch bugs early and provides confidence in the functionality of your changes. If you are not sure how to write tests, please reach out to the maintainers for guidance.
  - **Keep It Simple**: Strive for simplicity in your code. Avoid unnecessary complexity and aim for clear and straightforward solutions. Simple code is easier to maintain and understand.
    - **Use the DRY Principle**: Avoid duplicating code. If you find yourself writing the same code in multiple places, consider creating a reusable function or module. This promotes code reusability and reduces maintenance overhead.
    - **Easy to read code is better than clever and highly optimised code!**
    - If a constant is expected to keep changing, it should be a variable instead of a constant. Constants are for values that are expected to remain unchanged throughout the execution of the program.
  - **Naming conventions**: 
    - Use camelCase for variable and function names, and PascalCase for class names. 
    - Use SCREAMING_SNAKE_CASE for constants, configurations, and environment variables.
    - Use SCREAMING_SNAKE_CASE for md files. For example, use `README.md` instead of `readme.md` or `ReadMe.md`. 
    - Use underscores for file names and avoid spaces or special characters. For example, use `my_variable` instead of `my variable` or `my-variable`. 
    - Use hyphens for folder names and avoid spaces or special characters. For example, use `my-folder` instead of `my folder` or `my_folder`.
  - **Indentation and whitespace conventions**:
    - Use 4 spaces for indentation. Avoid using tabs or mixing spaces and tabs for indentation.
    - Use a single space after commas, colons, and semicolons.
    - Use a single blank line to separate logical sections of code, such as functions.
    - Use double blank lines to separate classes or major sections of code.
    - Do not use trailing whitespace at the end of lines.
    - Do not add any blank lines within functions or methods WHATSOEVER.
    - Avoid excessive blank lines that can make the code harder to read.

### Reporting Issues
If you encounter any issues or bugs while using the project, please report them by creating a new issue in the GitHub repository. Provide a clear and detailed description of the problem, including steps to reproduce it, any error messages you received, and any relevant screenshots or logs. This will help the maintainers understand the issue and work towards resolving it. If you have suggestions for improvements or new features, please feel free to submit them as well. Your feedback is valuable and helps improve the project for everyone.

  - **Before reporting an issue, please check the existing issues to see if it has already been reported.** If you find a similar issue, you can add your comments or additional information to help the maintainers understand the problem better.
  - **Contact information**: If you need to contact the maintainers directly, please use the contact information provided in the repository or reach out through the project's communication channels. However, please note that the maintainers are volunteers and may not be able to respond immediately. Be patient and allow some time for them to get back to you.

### On AI Use
When contributing to this project, please be mindful of the use of AI tools. While AI can be a helpful resource for generating code snippets or providing suggestions, it is important to ensure that the code you submit is your own work and adheres to the project's coding standards. Avoid submitting code that is generated entirely by AI without proper understanding or review, as this may lead to issues with code quality and maintainability. **It is conceivable that AI-generated code does generally not contain syntax errors, but it may not really reflect what you intended to do, even if your AI agent emphasises that it does. Always review and test any code generated by AI before submitting it to the project.**

### Thank You for Contributing!