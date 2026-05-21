module Posts
  class PublishPost
    def initialize(post)
      @post = post
    end

    def call
      return Result.failure("already published") if @post.published?

      @post.update!(published_at: Time.current, status: "published")
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
