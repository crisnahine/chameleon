module Comments
  class ModerateComment
    def initialize(comment, action:)
      @comment = comment
      @action = action
    end

    def call
      case @action
      when "approve" then @comment.update!(status: "approved")
      when "reject"  then @comment.update!(status: "rejected")
      else Result.failure("unknown action: #{@action}")
      end
    end
  end
end
